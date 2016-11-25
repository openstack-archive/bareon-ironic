#
# Copyright 2016 Cray Inc., All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import errno
import functools

from oslo_service import loopingcall

from bareon_ironic import exception as exc


class ProcTerminator(object):
    _poll_delay = 0.5
    is_terminated = False
    is_forced = False

    def __init__(self, proc, timeout=10, force_timeout=2, force=True):
        self.proc = proc
        self._actions_queue = [
            (self._do_terminate, timeout)]
        if force:
            self._actions_queue.append(
                (functools.partial(
                    self._do_terminate, force=True), force_timeout))

    def __call__(self):
        action_iterator = loopingcall.DynamicLoopingCall(self._do_action)
        action = action_iterator.start()
        poll_iterator = loopingcall.FixedIntervalLoopingCall(
            self._do_poll, action)
        poll = poll_iterator.start(self._poll_delay)
        try:
            poll.wait()
            if self.is_terminated:
                action_iterator.stop()
            else:
                action_iterator.wait()
        except OSError as e:
            if e.errno != errno.ESRCH:
                raise

    def _do_poll(self, action):
        if action.ready():
            raise loopingcall.LoopingCallDone()

        rcode = self.proc.poll()
        if rcode is None:
            return

        self.is_terminated = True
        raise loopingcall.LoopingCallDone()

    def _do_action(self):
        try:
            action, timeout = self._actions_queue.pop(0)
        except IndexError:
            raise exc.SurvivedSubprocess(pid=self.proc.pid)
        action()

        return timeout

    def _do_terminate(self, force=False):
        if not force:
            self.proc.terminate()
            return

        self.proc.kill()
        self.is_forced = True
