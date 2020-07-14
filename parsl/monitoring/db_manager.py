import logging
import threading
import queue
import os
import time
import datetime

from typing import Any, Dict, Set

from parsl.log_utils import set_file_logger
from parsl.dataflow.states import States
from parsl.providers.error import OptionalModuleMissing
from parsl.monitoring.message_type import MessageType
from parsl.process_loggers import wrap_with_logs

logger = logging.getLogger("database_manager")

try:
    import sqlalchemy as sa
    from sqlalchemy import Column, Text, Float, Boolean, Integer, DateTime, PrimaryKeyConstraint
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.ext.declarative import declarative_base
except ImportError:
    _sqlalchemy_enabled = False
else:
    _sqlalchemy_enabled = True

try:
    from sqlalchemy_utils import get_mapper
except ImportError:
    _sqlalchemy_utils_enabled = False
else:
    _sqlalchemy_utils_enabled = True

WORKFLOW = 'workflow'    # Workflow table includes workflow metadata
TASK = 'task'            # Task table includes task metadata
TRY = 'try'
STATUS = 'status'        # Status table includes task status
RESOURCE = 'resource'    # Resource table includes task resource utilization
NODE = 'node'            # Node table include node info


class Database:

    if not _sqlalchemy_enabled:
        raise OptionalModuleMissing(['sqlalchemy'],
                                    ("Default database logging requires the sqlalchemy library."
                                     " Enable monitoring support with: pip install parsl[monitoring]"))
    if not _sqlalchemy_utils_enabled:
        raise OptionalModuleMissing(['sqlalchemy_utils'],
                                    ("Default database logging requires the sqlalchemy_utils library."
                                     " Enable monitoring support with: pip install parsl[monitoring]"))

    Base = declarative_base()

    def __init__(self,
                 url='sqlite:///monitoring.db',
                 username=None,
                 password=None,
                 ):

        self.eng = sa.create_engine(url)
        self.meta = self.Base.metadata

        self.meta.create_all(self.eng)
        self.meta.reflect(bind=self.eng)

        Session = sessionmaker(bind=self.eng)
        self.session = Session()

    # BUG?: is this default None for messages a type error? None is not iterable,
    # but _generate_mappings iterates over it. and if so, why am I not
    # discovering this in the mypy branch?
    def update(self, table=None, columns=None, messages=None):
        logger.debug("Generating mappings for update of table {} for {} messages".format(table, len(messages)))
        table = self.meta.tables[table]
        mappings = self._generate_mappings(table, columns=columns,
                                           messages=messages)
        mapper = get_mapper(table)
        self.session.bulk_update_mappings(mapper, mappings)
        self.session.commit()
        logger.debug("Generated mappings for table {} for {} messages".format(table, len(messages)))

    def insert(self, table=None, messages=None):
        logger.debug("Generating mappings for insert into table {} for {} messages".format(table, len(messages)))
        table = self.meta.tables[table]
        mappings = self._generate_mappings(table, messages=messages)
        mapper = get_mapper(table)
        self.session.bulk_insert_mappings(mapper, mappings)
        self.session.commit()
        logger.debug("Generated mappings for table {} for {} messages".format(table, len(messages)))

    # TODO: this call, the calls to commit, and the related exception handling
    # should be moved into a `with` context manager (see @contextmanager decoration)
    # but also check ... does this rollback do sqlite3 ROLLBACK? and if so,
    # is a transaction actually started that this can meaningfully
    # rollback? looks like with sqlalchemy, transaction begins are implicit, so
    # "yes" but I could check. Could also assert/log that we aren't inside a
    # transaction at the point that we begin, so that we can detect if a
    # rollback will rollback further than expected.
    def rollback(self):
        self.session.rollback()

    def _generate_mappings(self, table, columns=None, messages=[]):
        logger.debug("starting _generate_mappings for table {}".format(table))
        mappings = []
        for msg in messages:
            logger.debug("Generating mapping for message {}, table {}".format(msg, table))
            m = {}
            if columns is None:
                columns = table.c.keys()
            for column in columns:
                m[column] = msg.get(column, None)
            logger.debug("Mapping is {}".format(m))
            mappings.append(m)
        logger.debug("ending _generate_mappings for table {}".format(table))
        return mappings

    class Workflow(Base):
        __tablename__ = WORKFLOW
        run_id = Column(Text, nullable=False, primary_key=True)
        workflow_name = Column(Text, nullable=True)
        workflow_version = Column(Text, nullable=True)
        time_began = Column(DateTime, nullable=False)
        time_completed = Column(DateTime, nullable=True)
        host = Column(Text, nullable=False)
        user = Column(Text, nullable=False)
        rundir = Column(Text, nullable=False)

        # are these values obtainable from simple SQL?
        # yes... but these are coming from the DFK somehow.
        tasks_failed_count = Column(Integer, nullable=False)
        tasks_completed_count = Column(Integer, nullable=False)

    # status represents state (transitions) of a task (not a try)
    # which will (in a complete execution) end in a terminal state.
    # do some of these rows need a try crossreference (not as part of PK)
    # so that status transitions associated with a particular try can happen?

    # TODO: the foreign key should be (try_id, task_id, run_id) together rather than
    # as separates - see how the primary key constraint works
    class Status(Base):
        __tablename__ = STATUS
        task_id = Column(Integer, sa.ForeignKey(
            'task.task_id'), nullable=False)
        task_status_name = Column(Text, nullable=False)
        timestamp = Column(DateTime, nullable=False)
        run_id = Column(Text, sa.ForeignKey('workflow.run_id'), nullable=False)
        try_id = Column('try_id', Integer, nullable=False)
        __table_args__ = (
            PrimaryKeyConstraint('task_id', 'run_id',
                                 'task_status_name', 'timestamp'),
        )

    class Task(Base):
        __tablename__ = TASK
        task_id = Column('task_id', Integer, nullable=False)
        run_id = Column('run_id', Text, nullable=False)

        task_depends = Column('task_depends', Text, nullable=True)
        task_func_name = Column('task_func_name', Text, nullable=False)
        task_memoize = Column('task_memoize', Text, nullable=False)
        task_hashsum = Column('task_hashsum', Text, nullable=True)
        task_inputs = Column('task_inputs', Text, nullable=True)
        task_outputs = Column('task_outputs', Text, nullable=True)
        task_stdin = Column('task_stdin', Text, nullable=True)
        task_stdout = Column('task_stdout', Text, nullable=True)
        task_stderr = Column('task_stderr', Text, nullable=True)

        # this times are a bit of a tangle and might split out into
        # something in 'status' (to capture multiple runs) for task_time_running
        # to properly capture the ability to start running multiple times
        # (and in the same way, capture multiple hostnames?)

        # task_time_submitted:   time_submitted field from dfk task record
        # ... which is the time the task was "launched". "launched" here means:
        # ready to be passed to an executor, but maybe we didn't pass to an
        # executor because of memoization. so sometimes is around the same time
        # as DFK.state goes to States.launched, but not always as memoization
        # might happen and we'd go straight from pending to failed/done. This may
        # be set multiple times in the presence of retries, because a task may be
        # launched multiple times in the presence of retries. Which is some data
        # loss that would be better handled in the status table, perhaps? -- this
        # is "try" level not "task" level

        # task_time_running - this is a message timestamp from some kind of
        # message where the first_msg flag is set. This looks like it is from
        # a different clock to the others - it is from the executor host clock,
        # not the submit host clock. this is "try" level not "task" level

        # task_time_returned    time_returned from dfk task record, which is
        # when the task went into 'done' state (according to the DFK-side clock)...
        # so does that happen for failing tasks? it looks like maybe no?
        # This is "task" not "try" level.
        # I think the db should be changed to:
        #   * task table: app invocation/result being available: start/end times (2 times, DFK clock)
        #     end time reflects going into *any* final state
        #     BUT potentially this info is acquirable from a joined states table
        #   * try table:
        #     start and end of try (so roughly submit to execution/executor future completing in either fail or success)
        #   * how to accomodate other-sourced info such as wrapper giving the
        #     `task_time_running` value? That's a bit like a State value but not entirely.
        #     Maybe it lines up with the resource table.
        #     matching that (potentially inconsistent data) with task/try table could be done on the query side.

        task_time_returned = Column(
            'task_time_returned', DateTime, nullable=True)

        # the 'try' table will have some relevance here? rows in try table?
        task_fail_count = Column('task_fail_count', Integer, nullable=False)

        __table_args__ = (
            PrimaryKeyConstraint('task_id', 'run_id'),
        )

    class Try(Base):
        __tablename__ = TRY
        try_id = Column('try_id', Integer, nullable=False)
        task_id = Column('task_id', Integer, nullable=False)
        run_id = Column('run_id', Text, nullable=False)

        # this is try-relevant
        hostname = Column('hostname', Text, nullable=True)

        # this is try-relevant? executors might change on retries?
        # if that doesn't happen, then it is task-relevant
        task_executor = Column('task_executor', Text, nullable=False)

        task_time_submitted = Column(
            'task_time_submitted', DateTime, nullable=True)

        # this comes from monitoring system, not from DFK
        task_time_running = Column(
            'task_time_running', DateTime, nullable=True)

        task_try_time_returned = Column(
            'task_try_time_returned', DateTime, nullable=True)

        # this should turn into a text field with only the current
        # failure (if there is one) rather than concatenated messages.
        task_fail_history = Column('task_fail_history', Text, nullable=True)

        __table_args__ = (
            PrimaryKeyConstraint('try_id', 'task_id', 'run_id'),
        )

    class Node(Base):
        __tablename__ = NODE
        id = Column('id', Integer, nullable=False, primary_key=True, autoincrement=True)
        run_id = Column('run_id', Text, nullable=False)
        hostname = Column('hostname', Text, nullable=False)
        cpu_count = Column('cpu_count', Integer, nullable=False)
        total_memory = Column('total_memory', Integer, nullable=False)
        active = Column('active', Boolean, nullable=False)
        worker_count = Column('worker_count', Integer, nullable=False)
        python_v = Column('python_v', Text, nullable=False)
        reg_time = Column('reg_time', DateTime, nullable=False)

    class Resource(Base):
        __tablename__ = RESOURCE
        try_id = Column('try_id', Integer, sa.ForeignKey(
            'try.try_id'), nullable=False)
        task_id = Column('task_id', Integer, sa.ForeignKey(
            'task.task_id'), nullable=False)
        run_id = Column('run_id', Text, sa.ForeignKey(
            'workflow.run_id'), nullable=False)
        timestamp = Column('timestamp', DateTime, nullable=False)
        resource_monitoring_interval = Column(
            'resource_monitoring_interval', Float, nullable=True)
        psutil_process_pid = Column(
            'psutil_process_pid', Integer, nullable=True)
        psutil_process_cpu_percent = Column(
            'psutil_process_cpu_percent', Float, nullable=True)
        psutil_process_memory_percent = Column(
            'psutil_process_memory_percent', Float, nullable=True)
        psutil_process_children_count = Column(
            'psutil_process_children_count', Float, nullable=True)
        psutil_process_time_user = Column(
            'psutil_process_time_user', Float, nullable=True)
        psutil_process_time_system = Column(
            'psutil_process_time_system', Float, nullable=True)
        psutil_process_memory_virtual = Column(
            'psutil_process_memory_virtual', Float, nullable=True)
        psutil_process_memory_resident = Column(
            'psutil_process_memory_resident', Float, nullable=True)
        psutil_process_disk_read = Column(
            'psutil_process_disk_read', Float, nullable=True)
        psutil_process_disk_write = Column(
            'psutil_process_disk_write', Float, nullable=True)
        psutil_process_status = Column(
            'psutil_process_status', Text, nullable=True)
        __table_args__ = (
            PrimaryKeyConstraint('try_id', 'task_id', 'run_id', 'timestamp'),
        )


class DatabaseManager:
    def __init__(self,
                 db_url='sqlite:///monitoring.db',
                 logdir='.',
                 logging_level=logging.INFO,
                 batching_interval=1,
                 batching_threshold=99999,
                 ):

        self.workflow_end = False
        self.workflow_start_message = None
        self.logdir = logdir
        os.makedirs(self.logdir, exist_ok=True)

        set_file_logger("{}/database_manager.log".format(self.logdir), level=logging_level,
                        format_string="%(asctime)s.%(msecs)03d %(name)s:%(lineno)d [%(levelname)s] [%(threadName)s %(thread)d] %(message)s",
                        name="database_manager")

        logger.debug("Initializing Database Manager process")

        self.db = Database(db_url)
        self.batching_interval = batching_interval
        self.batching_threshold = batching_threshold

        self.pending_priority_queue = queue.Queue()
        self.pending_node_queue = queue.Queue()
        self.pending_resource_queue = queue.Queue()

    def start(self, priority_queue, node_queue, resource_queue) -> None:

        self._kill_event = threading.Event()
        self._priority_queue_pull_thread = threading.Thread(target=self._migrate_logs_to_internal,
                                                            args=(
                                                                priority_queue, 'priority', self._kill_event,),
                                                            name="Monitoring-migrate-priority",
                                                            daemon=True,
                                                            )
        self._priority_queue_pull_thread.start()

        self._node_queue_pull_thread = threading.Thread(target=self._migrate_logs_to_internal,
                                                        args=(
                                                            node_queue, 'node', self._kill_event,),
                                                        name="Monitoring-migrate-node",
                                                        daemon=True,
                                                        )
        self._node_queue_pull_thread.start()

        self._resource_queue_pull_thread = threading.Thread(target=self._migrate_logs_to_internal,
                                                            args=(
                                                                resource_queue, 'resource', self._kill_event,),
                                                            name="Monitoring-migrate-resource",
                                                            daemon=True,
                                                            )
        self._resource_queue_pull_thread.start()

        """
        maintain a set to track the tasks that are already INSERTed into database
        to prevent race condition that the first resource message (indicate 'running' state)
        arrives before the first task message. In such a case, the resource table
        primary key would be violated.
        If that happens, the message will be added to deferred_resource_messages and processed later.

        """
        inserted_tasks = set()  # type: Set[object]

        """
        like inserted_tasks but for task,try tuples
        """
        inserted_tries = set()  # type: Set[Any]

        # for any task ID, we can defer exactly one message, which is the
        # assumed-to-be-unique first message (with first message flag set).
        # The code prior to this patch will discard previous message in
        # the case of multiple messages to defer.
        deferred_resource_messages = {}  # type: Dict[str, Any]

        while (not self._kill_event.is_set() or
               self.pending_priority_queue.qsize() != 0 or self.pending_resource_queue.qsize() != 0 or
               priority_queue.qsize() != 0 or resource_queue.qsize() != 0):

            """
            WORKFLOW_INFO and TASK_INFO messages (i.e. priority messages)

            """
            logger.debug("""Checking STOP conditions: {}, {}, {}, {}, {}""".format(
                              self._kill_event.is_set(),
                              self.pending_priority_queue.qsize() != 0, self.pending_resource_queue.qsize() != 0,
                              priority_queue.qsize() != 0, resource_queue.qsize() != 0))

            # This is the list of resource messages which can be reprocessed as if they
            # had just arrived because the corresponding first task message has been
            # processed (corresponding by task id)
            reprocessable_first_resource_messages = []

            # Get a batch of priority messages
            priority_messages = self._get_messages_in_batch(self.pending_priority_queue,
                                                            interval=self.batching_interval,
                                                            threshold=self.batching_threshold)
            if priority_messages:
                logger.debug(
                    "Got {} messages from priority queue".format(len(priority_messages)))
                task_info_update_messages, task_info_insert_messages, task_info_all_messages = [], [], []
                try_update_messages, try_insert_messages, try_all_messages = [], [], []
                for msg_type, msg in priority_messages:
                    if msg_type.value == MessageType.WORKFLOW_INFO.value:
                        if "python_version" in msg:   # workflow start message
                            # TODO: the start message should be indicated by a proper
                            # flag or test if we have seen the workflow before, not a
                            # magic other field that is about something else
                            logger.debug(
                                "Inserting workflow start info to WORKFLOW table")
                            self._insert(table=WORKFLOW, messages=[msg])
                            self.workflow_start_message = msg
                        else:                         # workflow end message
                            logger.debug(
                                "Updating workflow end info to WORKFLOW table")
                            self._update(table=WORKFLOW,
                                         columns=['run_id', 'tasks_failed_count',
                                                  'tasks_completed_count', 'time_completed'],
                                         messages=[msg])
                            self.workflow_end = True

                    elif msg_type.value == MessageType.TASK_INFO.value:
                        task_try_id = str(msg['task_id']) + "." + str(msg['try_id'])
                        task_info_all_messages.append(msg)
                        if msg['task_id'] in inserted_tasks:
                            task_info_update_messages.append(msg)
                        else:
                            inserted_tasks.add(msg['task_id'])
                            task_info_insert_messages.append(msg)

                        try_all_messages.append(msg)
                        if task_try_id in inserted_tries:
                            try_update_messages.append(msg)
                        else:
                            inserted_tries.add(task_try_id)
                            try_insert_messages.append(msg)

                            # check if there is a left_message for this task
                            if task_try_id in deferred_resource_messages:
                                reprocessable_first_resource_messages.append(
                                    deferred_resource_messages.pop(task_try_id))
                    else:
                        raise RuntimeError("Unexpected message type {} received on priority queue".format(msg_type))

                logger.debug("Updating and inserting TASK_INFO to all tables")
                logger.debug("Updating {} TASK_INFO into workflow table".format(len(task_info_update_messages)))
                # previously this only happened on task UPDATEs, but to simplify control flow,
                # i'm now doing that on any kind of TASK_INFO message. It is probably correct
                # to assume that these numbers don't update on a TASK_INFO for a new task,
                # but that isn't true if other stats are added in here.
                self._update(table=WORKFLOW,
                             columns=['run_id', 'tasks_failed_count',
                                      'tasks_completed_count'],
                             messages=task_info_all_messages)

                if task_info_insert_messages:
                    logger.debug("Inserting {} TASK_INFO to task table".format(len(task_info_insert_messages)))
                    self._insert(table=TASK, messages=task_info_insert_messages)
                    logger.debug(
                        "There are {} inserted task records".format(len(inserted_tasks)))

                if task_info_update_messages:
                    logger.debug("Updating {} TASK_INFO into task table".format(len(task_info_update_messages)))
                    # i am unclear if it is right to list the names of fields here
                    # rather than have them put into the task record at sender
                    # side if they should be updated, and then update every field
                    # in every table that matches? that would put more of the update
                    # decision into the sender side and less into the DB layer
                    # which would become less aware of the nuances of the connection
                    # between the DB schema and what is happening in the DFK.
                    self._update(table=TASK,
                                 columns=['task_time_submitted',
                                          'task_time_returned',
                                          'run_id', 'task_id',
                                          'task_fail_count'],
                                 messages=task_info_update_messages)
                logger.debug("Inserting {} task_info_all_messages into status table".format(len(task_info_all_messages)))

                self._insert(table=STATUS, messages=task_info_all_messages)

                if try_insert_messages:
                    logger.debug("Inserting {} TASK_INFO to try table".format(len(try_insert_messages)))
                    self._insert(table=TRY, messages=try_insert_messages)
                    logger.debug(
                        "There are {} inserted task records".format(len(inserted_tasks)))

                if try_update_messages:
                    logger.debug("Updating {} TASK_INFO into try table".format(len(try_update_messages)))
                    self._update(table=TRY,
                                 columns=['task_time_returned',
                                          'run_id', 'task_id', 'try_id',
                                          'task_fail_history',
                                          'task_time_submitted',
                                          'task_try_time_returned'],
                                 messages=try_update_messages)

            """
            NODE_INFO messages

            """
            node_info_messages = self._get_messages_in_batch(self.pending_node_queue,
                                                             interval=self.batching_interval,
                                                             threshold=self.batching_threshold)
            if node_info_messages:
                logger.debug(
                    "Got {} messages from node queue".format(len(node_info_messages)))
                self._insert(table=NODE, messages=node_info_messages)

            """
            Resource info messages

            """
            resource_messages = self._get_messages_in_batch(self.pending_resource_queue,
                                                            interval=self.batching_interval,
                                                            threshold=self.batching_threshold)

            if resource_messages:
                logger.debug(
                    "Got {} messages from resource queue, {} reprocessable".format(len(resource_messages), len(reprocessable_first_resource_messages)))
                self._insert(table=RESOURCE, messages=resource_messages)
                for msg in resource_messages:
                    task_try_id = str(msg['task_id']) + "." + str(msg['try_id'])
                    if msg['first_msg']:

                        msg['task_status_name'] = States.running.name
                        msg['task_time_running'] = msg['timestamp']

                        if task_try_id in inserted_tries:  # TODO: needs to become task_id and try_id, and check against inserted_tries
                            reprocessable_first_resource_messages.append(msg)
                        else:
                            if task_try_id in deferred_resource_messages:
                                logger.error("Task {} already has a deferred resource message. Discarding previous message.".format(msg['task_id']))
                            deferred_resource_messages[task_try_id] = msg

            if reprocessable_first_resource_messages:
                self._insert(table=STATUS, messages=reprocessable_first_resource_messages)
                self._update(table=TRY,
                             columns=['task_time_running',
                                      'run_id', 'task_id', 'try_id',
                                      'hostname'],
                             messages=reprocessable_first_resource_messages)

    # this function is specialised on queue tag, and reformats the messages expecting
    # a different format inside each one. that might not be the clearest way to implement
    # this.
    def _migrate_logs_to_internal(self, logs_queue, queue_tag, kill_event):
        logger.info("Starting processing for queue {}".format(queue_tag))

        while not kill_event.is_set() or logs_queue.qsize() != 0:
            logger.debug("""Checking STOP conditions for {} threads: {}, {}"""
                         .format(queue_tag, kill_event.is_set(), logs_queue.qsize() != 0))
            try:
                x, addr = logs_queue.get(timeout=0.1)   # addr is unused... could be tidied?
            except queue.Empty:
                continue
            else:
                if queue_tag == 'priority':
                    if x == 'STOP':
                        self.close()
                    else:
                        self.pending_priority_queue.put(x)
                elif queue_tag == 'resource':
                    self.pending_resource_queue.put(x[-1])  # put last element of data (the message) ignoring the other fields (id, time)
                elif queue_tag == 'node':
                    self.pending_node_queue.put(x[-1])

    def _update(self, table, columns, messages):
        try:
            self.db.update(table=table, columns=columns, messages=messages)
        except KeyboardInterrupt:
            logger.exception("KeyboardInterrupt when trying to update Table {}".format(table))
            try:
                self.db.rollback()
            except Exception:
                logger.exception("Rollback failed")
            raise
        except Exception:
            logger.exception("Got exception when trying to update table {}".format(table))
            try:
                self.db.rollback()
            except Exception:
                logger.exception("Rollback failed")

    def _insert(self, table, messages):
        try:
            self.db.insert(table=table, messages=messages)
        except KeyboardInterrupt:
            logger.exception("KeyboardInterrupt when trying to update Table {}".format(table))
            try:
                self.db.rollback()
            except Exception:
                logger.exception("Rollback failed")
            raise
        except Exception:
            logger.exception("Got exception when trying to insert to table {}".format(table))
            try:
                self.db.rollback()
            except Exception:
                logger.exception("Rollback failed")

    def _get_messages_in_batch(self, msg_queue, interval=1, threshold=99999):
        messages = []
        start = time.time()
        while True:
            if time.time() - start >= interval or len(messages) >= threshold:
                break
            try:
                x = msg_queue.get(timeout=0.1)
                # logger.debug("Database manager receives a message {}".format(x))
            except queue.Empty:
                logger.debug("Database manager has not received any message.")
                break
            else:
                messages.append(x)
        return messages

    def close(self):
        logger.info("Database Manager cleanup initiated.")
        if not self.workflow_end and self.workflow_start_message:
            logger.info("Logging workflow end info to database due to abnormal exit")
            time_completed = datetime.datetime.now()
            msg = {'time_completed': time_completed,
                   'workflow_duration': (time_completed - self.workflow_start_message['time_began']).total_seconds()}
            self.workflow_start_message.update(msg)
            self._update(table=WORKFLOW,
                         columns=['run_id', 'time_completed',
                                  'workflow_duration'],
                         messages=[self.workflow_start_message])
        self.batching_interval, self.batching_threshold = float(
            'inf'), float('inf')
        self._kill_event.set()


@wrap_with_logs
def dbm_starter(exception_q, priority_msgs, node_msgs, resource_msgs, *args, **kwargs):
    """Start the database manager process

    The DFK should start this function. The args, kwargs match that of the monitoring config

    """
    try:
        dbm = DatabaseManager(*args, **kwargs)
        logger.info("Starting dbm in dbm starter")
        dbm.start(priority_msgs, node_msgs, resource_msgs)
    except KeyboardInterrupt:
        logger.exception("KeyboardInterrupt signal caught")
        dbm.close()
        raise
    except Exception as e:
        logger.exception("dbm.start exception")
        exception_q.put(("DBM", str(e)))
        dbm.close()

    logger.info("End of dbm_starter")
