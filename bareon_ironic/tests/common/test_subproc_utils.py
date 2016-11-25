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
import tempfile

import mock

from bareon_ironic import exception as exc
from bareon_ironic.common import subproc_utils
from bareon_ironic.tests import base


class ProcTerminatorTestCase(base.AbstractTestCase):
    def test(self):
        proc = DummyPopen()

        terminator = subproc_utils.ProcTerminator(proc)
        terminator()
        self.assertEqual(proc.term_rcode, proc.rcode)

    def test_killed_in_advance(self):
        proc = DummyPopen()
        proc.terminate()

        terminator = subproc_utils.ProcTerminator(proc)
        terminator()
        self.assertEqual(proc.term_rcode, proc.rcode)

    def test_ignore_TERM(self):
        proc = DummyPopen()
        proc.terminate = mock.Mock(spec=proc.terminate)

        terminator = subproc_utils.ProcTerminator(
            proc, timeout=.01, force_timeout=.01)
        terminator._poll_delay = 0.001
        terminator()
        self.assertEqual(True, terminator.is_terminated)
        self.assertEqual(True, terminator.is_forced)

    def test_ignore_TERM_without_force(self):
        proc = DummyPopen()
        proc.terminate = mock.Mock(spec=proc.terminate)

        terminator = subproc_utils.ProcTerminator(
            proc, timeout=.01, force=False)
        terminator._poll_delay = 0.1
        self.assertRaises(exc.SurvivedSubprocess, terminator)
        self.assertEqual(False, terminator.is_terminated)
        self.assertEqual(False, terminator.is_forced)

    def test_ignore_KILL(self):
        """Process dies too slow"""

        proc = DummyPopen()
        proc.terminate = mock.Mock(spec=proc.terminate)
        proc.kill = mock.Mock(spec=proc.kill)

        terminator = subproc_utils.ProcTerminator(
            proc, timeout=.01, force_timeout=.01)
        terminator._poll_delay = 0.001
        self.assertRaises(exc.SurvivedSubprocess, terminator)
        self.assertEqual(False, terminator.is_terminated)
        self.assertEqual(True, terminator.is_forced)

    def test_missing_proc(self):
        proc = DummyPopen()
        proc.terminate = mock.Mock(
            side_effect=OSError(errno.ESRCH, 'Fake error'))

        terminator = subproc_utils.ProcTerminator(proc)
        terminator()
        self.assertEqual(False, terminator.is_terminated)

    def test_insufficient_access(self):
        proc = DummyPopen()
        proc.terminate = mock.Mock(
            side_effect=OSError(errno.EPERM, 'Fake error'))

        terminator = subproc_utils.ProcTerminator(proc)
        self.assertRaises(OSError, terminator)


class DummyPopen(object):
    def __init__(self, rcode=None, term_rcode=-15, kill_rcode=-9, pid=1234):
        self.rcode = rcode
        self.term_rcode = term_rcode
        self.kill_rcode = kill_rcode
        self.pid = pid

        self.rcode = None
        self.stdin = tempfile.TemporaryFile()
        self.stdout = tempfile.TemporaryFile()
        self.stderr = tempfile.TemporaryFile()

    def terminate(self):
        self.rcode = self.term_rcode

    def kill(self):
        self.rcode = self.kill_rcode

    def poll(self):
        return self.rcode
