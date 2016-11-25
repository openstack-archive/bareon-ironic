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
import itertools
import subprocess
import tempfile

import mock

from bareon_ironic import exception as exc
from bareon_ironic.modules import bareon_utils as utils
from bareon_ironic.tests import base


class SSHPortForwardingTestCase(base.AbstractTestCase):
    user = 'john-doe'
    key_file = '/path/to/john-doe/auth/keys/host-key.rsa'
    host = 'dummy.remote.local'
    bind = utils.NetAddr('127.0.1.2', 4080)
    forward = utils.NetAddr('127.3.4.5', 5080)

    def setUp(self):
        super(SSHPortForwardingTestCase, self).setUp()

        # must be created before mocking
        dummy_proc = DummyPopen()
        self.ssh_proc = mock.Mock(wraps=dummy_proc)

        self.mock = {}
        self.mock_patch = {
            'subprocess.Popen': mock.patch(
                'subprocess.Popen', return_value=self.ssh_proc),
            'tempfile.TemporaryFile': mock.patch(
                'tempfile.TemporaryFile', return_value=self.ssh_proc.stderr),
            'time.time': mock.patch(
                'time.time', side_effect=itertools.count()),
            'time.sleep': mock.patch('time.sleep'),
            'socket.socket': mock.patch('socket.socket')}

        for name in self.mock_patch:
            patch = self.mock_patch[name]
            self.mock[name] = patch.start()
            self.addCleanup(patch.stop)

        self.local_forwarding = utils.SSHLocalPortForwarding(
            self.user, self.key_file, self.host, self.bind, self.forward)
        self.remote_forwarding = utils.SSHRemotePortForwarding(
            self.user, self.key_file, self.host, self.bind, self.forward)

    @mock.patch.object(utils.SSHRemotePortForwarding, '_check_port_forwarding')
    def test_remote_without_validation(self, validate_method):
        self.remote_forwarding.validate_timeout = 0

        forward_argument = self.bind + self.forward
        forward_argument = [str(x) for x in forward_argument]
        forward_argument = ':'.join(forward_argument)
        user_host = '@'.join((self.user, self.host))

        with self.remote_forwarding:
            self.assertEqual(1, self.mock['subprocess.Popen'].call_count)
            popen_args, popen_kwargs = self.mock['subprocess.Popen'].call_args
            cmd = popen_args[0]
            self.assertEqual(['ssh'], cmd[:1])
            try:
                actual_forward = cmd[cmd.index('-R') + 1]
            except IndexError:
                raise AssertionError(
                    'Missing expected arguments -R <forward_spec> in SSH call')
            self.assertEqual(forward_argument, actual_forward)

            self.assertIn('-N', cmd)
            self.assertEqual(user_host, cmd[-1])

            self.assertIs(self.ssh_proc.stderr, popen_kwargs.get('stderr'))
            self.assertEqual(0, self.ssh_proc.terminate.call_count)

        self.assertEqual(0, validate_method.call_count)
        self.assertEqual(1, self.ssh_proc.terminate.call_count)

    @mock.patch('select.select')
    def test_remote_validation(self, select_mock):
        self.ssh_proc.stdout.write('CONNECT APPROVED\n')
        self.ssh_proc.stdout.seek(0)

        select_mock.return_value = [[self.ssh_proc.stdout.fileno()], [], []]
        with self.remote_forwarding:
            popen_args, popen_kwargs = self.mock['subprocess.Popen'].call_args
            cmd = popen_args[0]

            self.assertNotIn('-N', cmd)
            self.assertEqual(['python'], cmd[-1:])

            self.assertIs(subprocess.PIPE, popen_kwargs['stdout'])
            self.assertIs(subprocess.PIPE, popen_kwargs['stdin'])

            self.assertTrue(self.ssh_proc.stdin.closed)
            self.assertEqual(0, self.ssh_proc.terminate.call_count)
        self.assertEqual(1, self.ssh_proc.terminate.call_count)

    @mock.patch('select.select')
    def test_remote_validation_fail(self, select_mock):
        ssh_output_indicator = 'SSH output grabbing indicator'

        self.ssh_proc.stdout.write('output don\'t matching success marker\n')
        self.ssh_proc.stdout.seek(0)
        self.ssh_proc.stderr.write(ssh_output_indicator)

        select_mock.side_effect = itertools.chain(
            ([[self.ssh_proc.stdout.fileno()], [], []], ),
            itertools.repeat([[], [], []]))
        try:
            with self.remote_forwarding:
                pass
        except exc.SSHSetupForwardingError as e:
            self.assertIn(ssh_output_indicator, str(e))
        except Exception as e:
            raise AssertionError('Catch {!r} instead of {!r}'.format(
                e, exc.SSHSetupForwardingError))
        else:
            raise AssertionError(
                'There was no expected exception: {!r}'.format(
                    exc.SSHSetupForwardingError))

        self.assertEqual(1, self.ssh_proc.terminate.call_count)


class ProcTerminatorTestCase(base.AbstractTestCase):
    def test(self):
        proc = DummyPopen()

        terminator = utils.ProcTerminator(proc)
        terminator()
        self.assertEqual(proc.term_rcode, proc.rcode)

    def test_killed_in_advance(self):
        proc = DummyPopen()
        proc.terminate()

        terminator = utils.ProcTerminator(proc)
        terminator()
        self.assertEqual(proc.term_rcode, proc.rcode)

    def test_ignore_TERM(self):
        proc = DummyPopen()
        proc.terminate = mock.Mock(spec=proc.terminate)

        terminator = utils.ProcTerminator(proc, timeout=.01, force_timeout=.01)
        terminator._poll_delay = 0.001
        terminator()
        self.assertEqual(True, terminator.is_terminated)
        self.assertEqual(True, terminator.is_forced)

    def test_ignore_TERM_without_force(self):
        proc = DummyPopen()
        proc.terminate = mock.Mock(spec=proc.terminate)

        terminator = utils.ProcTerminator(proc, timeout=.01, force=False)
        terminator._poll_delay = 0.1
        self.assertRaises(exc.SurvivedSubprocess, terminator)
        self.assertEqual(False, terminator.is_terminated)
        self.assertEqual(False, terminator.is_forced)

    def test_ignore_KILL(self):
        """Process dies too slow"""

        proc = DummyPopen()
        proc.terminate = mock.Mock(spec=proc.terminate)
        proc.kill = mock.Mock(spec=proc.kill)

        terminator = utils.ProcTerminator(proc, timeout=.01, force_timeout=.01)
        terminator._poll_delay = 0.001
        self.assertRaises(exc.SurvivedSubprocess, terminator)
        self.assertEqual(False, terminator.is_terminated)
        self.assertEqual(True, terminator.is_forced)

    def test_missing_proc(self):
        proc = DummyPopen()
        proc.terminate = mock.Mock(
            side_effect=OSError(errno.ESRCH, 'Fake error'))

        terminator = utils.ProcTerminator(proc)
        terminator()
        self.assertEqual(False, terminator.is_terminated)

    def test_insufficient_access(self):
        proc = DummyPopen()
        proc.terminate = mock.Mock(
            side_effect=OSError(errno.EPERM, 'Fake error'))

        terminator = utils.ProcTerminator(proc)
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
