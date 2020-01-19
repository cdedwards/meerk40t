from threading import Lock, Thread
from LaserNode import LaserNode
import time

THREAD_STATE_UNKNOWN = -1
THREAD_STATE_UNSTARTED = 0
THREAD_STATE_STARTED = 1
THREAD_STATE_PAUSED = 2
THREAD_STATE_FINISHED = 3
THREAD_STATE_ABORT = 10


class KernelJob:
    def __init__(self, scheduler, process, args, interval=1.0, times=None):
        self.scheduler = scheduler
        self.interval = interval
        self.last_run = time.time()
        self.next_run = self.last_run + self.interval
        self.process = process
        self.args = args
        self.times = times
        self.paused = False
        self.executing = False

    @property
    def scheduled(self):
        return self.next_run is not None and time.time() >= self.next_run

    def run(self):
        if self.paused:
            return
        self.next_run = None

        if self.times is not None:
            self.times = self.times - 1
            if self.times <= 0:
                self.scheduler.jobs.remove(self)
        self.process(*self.args)
        self.last_run = time.time()
        self.next_run = self.last_run + self.interval


class Scheduler(Thread):
    def __init__(self, kernel):
        Thread.__init__(self)
        self.kernel = kernel
        self.state = THREAD_STATE_UNSTARTED
        self.jobs = []

    def add_job(self, run, args=(), interval=1.0, times=None):
        job = KernelJob(self, run, args, interval, times)
        self.jobs.append(job)
        return job

    def run(self):
        self.state = THREAD_STATE_STARTED
        while self.state != THREAD_STATE_ABORT:
            time.sleep(0.1)
            if self.state == THREAD_STATE_ABORT:
                return
            while self.state == THREAD_STATE_PAUSED:
                time.sleep(1.0)
            for job in self.jobs:
                if job.scheduled:
                    job.run()
        self.state = THREAD_STATE_FINISHED

    def state(self):
        return self.state

    def resume(self):
        self.state = THREAD_STATE_STARTED

    def pause(self):
        self.state = THREAD_STATE_PAUSED

    def stop(self):
        self.state = THREAD_STATE_ABORT


class Module:
    def __init__(self):
        self.kernel = None

    def initialize(self, kernel):
        self.kernel = kernel

    def shutdown(self, kernel):
        self.kernel = None


class Kernel:
    """
    The kernel is the software framework that is tasked with the API implementation.

    The class serves as:
    * a hub of communications between different processes.
    * implementation of event listeners
    * persistent saving and loading settings data
    * the threading of different events
    * interactions between different modules

    Since most parts of the system are intended to allow swapping between the different modules a level of API
    abstraction is required. The hackability of various systems depends on a robust code that is agnostic to the state
    of the rest of modules.

    LaserNode data is primarily of three types: path, image, and text.
    Readers convert some file or streams being read into LaserNode data.
    Saver, convert LaserNode data into some types of files or streams.
    Effects convert LaserNode data into different LaserNode data.
    Operations are functions that can be arbitrarily added to spoolers.
    Controls are functions that can simply be called, typically for very light threads.
    A spooler is a processing queue for LaserNode data and other command generators.
    Command generators use the LaserCommandConstant middle language to facilitate controller events.
    LaserNode are command generators.

    Most MeerK40t objects will have a direct reference to the Kernel. However, the Kernel should not directly reference
    any object.

    kernel.setting(type, name, default): Registers kernel.name as a persistent setting.
    kernel.flush(): push out persistent settings
    kernel.listen(signal, function): Registers function as a listener for the given signal.
    kernel.unlisten(signal, function): Unregister function as a listener for the signal.
    kernel.add_loader(loader): Registers a qualified loader function.
    kernel.add_saver(file): Registers a qualified saver function.
    kernel.add_effect(effect): registers a qualified effect.
    kernel.add_operation(op): registers a spooler operation
    kernel.add_thread(thread, object): Registers a thread.
    kernel.remove_thread(thread)

    kernel.load(file): Loads the given file and turns it into laser nodes.
    kernel.write(file): Saves the laser nodes to the given filename or stream.
    kernel.tick(seconds, function): tick each x seconds until function returns False.
    kernel.shutdown(): shuts down kernel.

    kernel(signal, value): Calls the signal with the given value.
    """

    def __init__(self, spooler=None, config=None):
        self.spooler = spooler  # Class responsible for spooling commands
        self.elements = LaserNode(parent=self)

        self.config = None

        self.modules = {}
        self.loaders = {}
        self.savers = {}
        self.threads = {}
        self.controls = {}
        self.windows = {}
        self.operations = []

        self.effects = []

        self.listeners = {}
        self.last_message = {}
        self.queue_lock = Lock()
        self.message_queue = {}
        self.is_queue_processing = False
        self.run_later = lambda listener, message: listener(message)

        self.selected = None
        self.open_windows = {}
        self.keymap = {}

        self.translation = lambda e: e  # Default for this code is do nothing.
        if config is not None:
            self.set_config(config)
        self.cron = None

    def __str__(self):
        return "Project"

    def __call__(self, code, message):
        self.signal(code, message)

    def signal(self, code, message):
        self.queue_lock.acquire(True)
        self.message_queue[code] = message
        self.queue_lock.release()

    def delegate_messages(self):
        if self.is_queue_processing:
            return
        self.run_later(self.process_queue, None)

    def process_queue(self, *args):
        self.is_queue_processing = True
        self.queue_lock.acquire(True)
        queue = self.message_queue
        self.message_queue = {}
        self.queue_lock.release()

        for code, message in queue.items():
            if code in self.listeners:
                listeners = self.listeners[code]
                for listener in listeners:
                    self.run_later(listener, message)
            self.last_message[code] = message
        self.is_queue_processing = False

    def __setitem__(self, key, value):
        if isinstance(key, tuple):
            if value is None:
                key, value = key
                self.unlisten(key, value)
            else:
                key, value = key
                self.listen(key, value)
        elif isinstance(key, str):
            self.write_config(key, value)

    def __getitem__(self, item):
        if isinstance(item, tuple):
            if len(item) == 2:
                t, key = item
                return self.read_config(t, key)
            else:
                t, key, default = item
                return self.read_config(t, key, default)
        return self.config.Read(item)

    def boot(self):
        self.cron = Scheduler(self)
        self.cron.add_job(self.delegate_messages, args=(), interval=0.05)
        self.add_thread('Scheduler', self.cron)
        self.cron.start()

    def add_module(self, module_name, module):
        self.modules[module_name] = module
        try:
            module.initialize(self)
        except AttributeError:
            pass

    def add_loader(self, loader_name, loader):
        self.loaders[loader_name] = loader

    def add_saver(self, saver_name, saver):
        self.savers[saver_name] = saver

    def read_config(self, t, key, default=None):
        if default is not None:
            if t == str:
                return self.config.Read(key, default)
            elif t == int:
                return self.config.ReadInt(key, default)
            elif t == float:
                return self.config.ReadFloat(key, default)
            elif t == bool:
                return self.config.ReadBool(key, default)
        if t == str:
            return self.config.Read(key)
        elif t == int:
            return self.config.ReadInt(key)
        elif t == float:
            return self.config.ReadFloat(key)
        elif t == bool:
            return self.config.ReadBool(key)

    def write_config(self, key, value):
        if isinstance(value, str):
            self.config.Write(key, value)
        elif isinstance(value, int):
            self.config.WriteInt(key, value)
        elif isinstance(value, float):
            self.config.WriteFloat(key, value)
        elif isinstance(value, bool):
            self.config.WriteBool(key, value)

    def set_config(self, config):
        self.config = config
        for attr in dir(self):
            if attr.startswith('_'):
                continue
            value = getattr(self, attr)
            if value is None:
                continue
            if isinstance(value, (int, bool, float, str)):
                self.write_config(attr, value)
        more, value, index = config.GetFirstEntry()
        while more:
            if not value.startswith('_'):
                if not hasattr(self, value):
                    setattr(self, value, None)
            more, value, index = config.GetNextEntry(index)

    def setting(self, setting_type, setting_name, default=None):
        """
        Registers a setting to be used between modules.

        If the setting exists, it's value remains unchanged.
        If the setting exists in the persistent storage that value is used.
        If there is no settings value, the default will be used.

        :param setting_type: int, float, str, or bool value
        :param setting_name: name of the setting
        :param default: default value for the setting to have.
        :return:
        """
        if hasattr(self, setting_name) and getattr(self, setting_name) is not None:
            return
        if not setting_name.startswith('_') and self.config is not None:
            load_value = self[setting_type, setting_name, default]
        else:
            load_value = default
        setattr(self, setting_name, load_value)

    def flush(self):
        for attr in dir(self):
            if attr.startswith('_'):
                continue
            value = getattr(self, attr)
            if value is None:
                continue
            if isinstance(value, (int, bool, str, float)):
                self[attr] = value

    def update(self, setting_name, value):
        if hasattr(self, setting_name):
            old_value = getattr(self, setting_name)
        else:
            old_value = None
        setattr(self, setting_name, value)
        self(setting_name, (value, old_value))

    def add_window(self, window_name, window):
        self.windows[window_name] = window

    def mark_window_closed(self, name):
        if name in self.open_windows:
            del self.open_windows[name]

    def close_old_window(self, name):
        if name in self.open_windows:
            try:
                self.open_windows[name].Close()
            except RuntimeError:
                pass  # already closed.
        self.mark_window_closed(name)

    def open_window(self, window_name):
        self.close_old_window(window_name)
        w = self.windows[window_name]
        window = w(None, -1, "")
        window.set_project(self)
        window.Show()
        self.open_windows[window_name] = window
        return window

    def shutdown(self):
        """
        Invokes Kernel Shutdown.
        All threads are stopped.
        """
        self.cron.stop()
        for module_name in self.modules:
            module = self.modules[module_name]
            try:
                module.stop()
            except AttributeError:
                pass
        for thread_name in self.threads:
            thread = self.threads[thread_name]
            while thread.is_alive():
                time.sleep(0.1)
                # print("Waiting for processes to stop. %s" % (str(thread)))
            # print("Thread has Finished: %s" % (str(thread)))

    def listen(self, signal, function):
        if signal in self.listeners:
            listeners = self.listeners[signal]
            listeners.append(function)
        else:
            self.listeners[signal] = [function]
        if signal in self.last_message:
            last_message = self.last_message[signal]
            function(last_message)

    def unlisten(self, listener, code):
        if code in self.listeners:
            listeners = self.listeners[code]
            listeners.remove(listener)

    def add_control(self, control_name, function):
        self.controls[control_name] = function

    def remove_control(self, control_name):
        del self.controls[control_name]

    def execute(self, control_name, *args):
        self.controls[control_name](*args)

    def add_thread(self, thread_name, object):
        self.threads[thread_name] = object

    def remove_thread(self, thread_name):
        del self.threads[thread_name]

    def get_state_string_from_state(self, state):
        if state == THREAD_STATE_UNSTARTED:
            return "Unstarted"
        elif state == THREAD_STATE_ABORT:
            return "Aborted"
        elif state == THREAD_STATE_FINISHED:
            return "Finished"
        elif state == THREAD_STATE_PAUSED:
            return "Paused"
        elif state == THREAD_STATE_STARTED:
            return "Started"
        elif state == THREAD_STATE_UNKNOWN:
            return "Unknown"

    def get_state(self, thread_name):
        try:
            return self.modules[thread_name].state()
        except AttributeError:
            return THREAD_STATE_UNKNOWN

    def start(self, thread_name):
        try:
            self.modules[thread_name].start()
        except AttributeError:
            pass

    def resume(self, thread_name):
        try:
            self.modules[thread_name].resume()
        except AttributeError:
            pass

    def pause(self, thread_name):
        try:
            self.modules[thread_name].pause()
        except AttributeError:
            pass

    def abort(self, thread_name):
        try:
            self.modules[thread_name].abort()
        except AttributeError:
            pass

    def reset(self, thread_name):
        try:
            self.modules[thread_name].reset()
        except AttributeError:
            pass

    def stop(self, thread_name):
        try:
            self.modules[thread_name].stop()
        except AttributeError:
            pass

    def set_selected(self, selected):
        self.selected = selected
        self("selection", self.selected)

    def notify_change(self):
        self("elements", 0)

    def move_selected(self, dx, dy):
        if self.selected is None:
            return
        self.selected.move(dx, dy)
        for e in self.selected:
            e.move(dx, dy)

    def load(self, pathname, group=None):
        for loader_name, loader in self.loaders.items():
            for description, extensions, mimetype in loader.load_types():
                if pathname.lower().endswith(extensions):
                    loader.load(pathname, group)
                    return True

    def load_types(self, all=True):
        filetypes = []
        if all:
            filetypes.append('All valid types')
            exts = []
            for loader_name, loader in self.loaders.items():
                for description, extensions, mimetype in loader.load_types():
                    for ext in extensions:
                        exts.append('*.%s' % ext)
            filetypes.append(';'.join(exts))
        for loader_name, loader in self.loaders.items():
            for description, extensions, mimetype in loader.load_types():
                exts = []
                for ext in extensions:
                    exts.append('*.%s' % ext)
                filetypes.append("%s (%s)" % (description, extensions[0]))
                filetypes.append(';'.join(exts))
        return "|".join(filetypes)

    def save(self, pathname):
        for save_name, saver in self.savers.items():
            for description, extension, mimetype in saver.save_types():
                if pathname.lower().endswith(extension):
                    saver.save(pathname, 'default')
                    return True

    def save_types(self):
        filetypes = []
        for saver_name, saver in self.savers.items():
            for description, extension, mimetype in saver.save_types():
                filetypes.append("%s (%s)" % (description, extension))
                filetypes.append(extension)
        return "|".join(filetypes)