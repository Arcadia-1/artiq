import logging

from artiq.protocols.sync_struct import Notifier


class LogBuffer:
    def __init__(self, depth):
        self.depth = depth
        self.data = Notifier([])

    def log(self, level, source, time, message):
        if len(self.data.read) >= self.depth:
            del self.data[0]
        self.data.append((level, source, time, message))


class LogBufferHandler(logging.Handler):
    def __init__(self, log_buffer, *args, **kwargs):
        logging.Handler.__init__(self, *args, **kwargs)
        self.log_buffer = log_buffer

    def emit(self, record):
        message = self.format(record)
        self.log_buffer.log(record.levelno, record.source, record.created, message)


name_to_level = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def parse_log_message(msg):
    for name, level in name_to_level.items():
        if msg.startswith(name + ":"):
            remainder = msg[len(name) + 1:]
            try:
                idx = remainder.index(":")
            except:
                continue
            return level, remainder[:idx], remainder[idx+1:]
    return logging.INFO, "print", msg


fwd_logger = logging.getLogger("fwd")


class LogForwarder:
    def log_worker(self, rid, message):
        level, name, message = parse_log_message(message)
        fwd_logger.name = name
        fwd_logger.log(level, message,
                       extra={"source": "worker({})".format(rid)})
    log_worker.worker_pass_rid = True


class SourceFilter:
    def __init__(self, master_level):
        self.master_level = master_level

    def filter(self, record):
        if not hasattr(record, "source"):
            record.source = "master"
        if record.source == "master":
            return record.levelno >= self.master_level
        else:
            # log messages that are forwarded from a source have already
            # been filtered, and may have a level below the master level.
            return True


def log_args(parser):
    group = parser.add_argument_group("verbosity")
    group.add_argument("-v", "--verbose", default=0, action="count",
                       help="increase logging level for the master process")
    group.add_argument("-q", "--quiet", default=0, action="count",
                       help="decrease logging level for the master process")


def init_log(args):
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.NOTSET)  # we use our custom filter only
    flt = SourceFilter(logging.WARNING + args.quiet*10 - args.verbose*10)

    handlers = []
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s:%(source)s:%(name)s:%(message)s"))
    handlers.append(console_handler)
    
    log_buffer = LogBuffer(1000)
    buffer_handler = LogBufferHandler(log_buffer)
    buffer_handler.setFormatter(logging.Formatter("%(name)s:%(message)s"))
    handlers.append(buffer_handler)

    for handler in handlers:
        handler.addFilter(flt)
        root_logger.addHandler(handler)

    log_forwarder = LogForwarder()

    return log_buffer, log_forwarder
