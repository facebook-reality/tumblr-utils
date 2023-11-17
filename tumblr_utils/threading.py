from typing import Callable, Generic, TypeVar, TYPE_CHECKING

import errno
import queue
import threading


class FakeGenericMeta(type):
    def __getitem__(cls, item):
        return cls


if TYPE_CHECKING:
    T = TypeVar('T')

    class GenericQueue(queue.Queue[T], Generic[T]):
        pass
else:
    T = None

    class GenericQueue(queue.Queue, metaclass=FakeGenericMeta):
        pass


class LockedQueue(GenericQueue[T]):
    def __init__(self, lock, maxsize=0):
        super().__init__(maxsize)
        self.mutex = lock
        self.not_empty = threading.Condition(lock)
        self.not_full = threading.Condition(lock)
        self.all_tasks_done = threading.Condition(lock)


class ThreadPool:
    queue: LockedQueue[Callable[[], None]]

    def __init__(self, max_queue=1000):
        self.queue = LockedQueue(main_thread_lock, max_queue)  # TODO: Figure out how to pass this RLock to the module during execution
        self.quit = threading.Condition(main_thread_lock)
        self.quit_flag = False
        self.abort_flag = False
        self.errors = False
        self.threads = [threading.Thread(target=self.handler) for _ in range(options.threads)]
        for t in self.threads:
            t.start()

    def add_work(self, *args, **kwargs):
        self.queue.put(*args, **kwargs)

    def wait(self):
        with multicond:
            self._print_remaining(self.queue.qsize())
            self.quit_flag = True
            self.quit.notify_all()
            while self.queue.unfinished_tasks:
                no_internet.check(release=True)
                enospc.check(release=True)
                # All conditions false, wait for a change
                multicond.wait((self.queue.all_tasks_done, no_internet.cond, enospc.cond))

    def cancel(self):
        with main_thread_lock:
            self.abort_flag = True
            self.quit.notify_all()
            no_internet.destroy()
            enospc.destroy()

        for i, t in enumerate(self.threads, start=1):
            logger.status('Stopping threads {}{}\r'.format(' ' * i, '.' * (len(self.threads) - i)))
            t.join()

        logger.info('Backup canceled.\n')

        with main_thread_lock:
            self.queue.queue.clear()
            self.queue.all_tasks_done.notify_all()

    def handler(self):
        def wait_for_work():
            while not self.abort_flag:
                if self.queue.qsize():
                    return True
                elif self.quit_flag:
                    break
                # All conditions false, wait for a change
                multicond.wait((self.queue.not_empty, self.quit))
            return False

        while True:
            with multicond:
                if not wait_for_work():
                    break
                work = self.queue.get(block=False)
                qsize = self.queue.qsize()
                if self.quit_flag and qsize % REM_POST_INC == 0:
                    self._print_remaining(qsize)

            try:
                while True:
                    try:
                        success = work()
                        break
                    except OSError as e:
                        if e.errno == errno.ENOSPC:
                            enospc.signal()
                            continue
                        raise
            finally:
                self.queue.task_done()
            if not success:
                self.errors = True

    @staticmethod
    def _print_remaining(qsize):
        if qsize:
            logger.status('{} remaining posts to save\r'.format(qsize))
        else:
            logger.status('Waiting for worker threads to finish\r')

