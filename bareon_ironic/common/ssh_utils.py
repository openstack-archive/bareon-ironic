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

import abc
import collections
import fcntl
import os
import select
import socket
import subprocess
import tempfile
import textwrap
import time

import six
from ironic.common import exception

from bareon_ironic import exception as exc
from bareon_ironic.common import subproc_utils

NetAddr = collections.namedtuple('NetAddr', ('host', 'port'))


@six.add_metaclass(abc.ABCMeta)
class _SSHPortForwardingAbstract(object):
    """Abstract base class for SSH port forwarding handlers

    Local and remote port forwarding management handlers are based on this
    abstract class. It implement context management protocol so it must be used
    inside "with" keyword. Port forwarding is activated into __enter__ method
    and deactivated into __exit__ method.
    """

    proc = None
    subprocess_output = None

    def __init__(self, user, key_file, host, bind, forward,
                 ssh_port=None, validate_timeout=30):
        """Collect and store data required for port forwarding setup

        :param user: username for SSH connections
        :type user: str
        :param key_file: path to private key for SSH authentication
        :type key_file: str
        :param host: address of remote hosts
        :type host: str
        :param bind: address/port pair to bind forwarded port
        :type bind: NetAddr
        :param forward: address/port pair defining destination of port
                        forwarding
        :type forward: NetAddr
        :param ssh_port: SSH port on remote host
        :type ssh_port: int
        :param validate_timeout: amount of time we will wait for creation of
                                 port forwarding. If set to zero - skip
                                 validation.
        :type validate_timeout: int
        """
        self.user = user
        self.key = key_file
        self.host = host
        self.bind = bind
        self.forward = forward
        self.ssh_port = ssh_port
        self.validate_timeout = max(validate_timeout, 0)

    def __repr__(self):
        setup = self._ssh_command_add_forward_arguments([])
        setup = ' '.join(setup)
        return '<{} {}>'.format(type(self).__name__, setup)

    def __enter__(self):
        self.subprocess_output = tempfile.TemporaryFile()

        cmd = self._make_ssh_command()
        proc_args = {
            'close_fds': True,
            'stderr': self.subprocess_output}
        proc_args = self._subprocess_add_args(proc_args)
        try:
            self.proc = subprocess.Popen(cmd, **proc_args)
            if self.validate_timeout:
                self._check_port_forwarding()
        except OSError as e:
            raise exc.SubprocessError(command=cmd, error=e)
        except exc.SSHForwardedPortValidationError as e:
            self._kill()
            remote = self._ssh_command_add_destination([])
            remote = ' '.join(str(x) for x in remote)
            raise exc.SSHSetupForwardingError(
                forward=self, error=e.message, remote=remote,
                output=self._grab_subprocess_output())
        except Exception:
            self._kill()
            raise

        return self

    def __exit__(self, *exc_info):
        del exc_info
        if self.proc.poll() is not None:
            raise exception.SSHConnectFailed(
                'Unexpected SSH process termination.\n'
                '{}'.format(self._grab_subprocess_output()))
        self._kill()

    def _make_ssh_command(self):
        cmd = ['ssh']
        cmd = self._ssh_command_add_auth_arguments(cmd)
        cmd = self._ssh_command_add_forward_arguments(cmd)
        cmd = self._ssh_command_add_extra_arguments(cmd)
        cmd = self._ssh_command_add_destination(cmd)
        cmd = self._ssh_command_add_command(cmd)

        return cmd

    def _ssh_command_add_auth_arguments(self, cmd):
        return cmd + ['-i', self.key]

    @abc.abstractmethod
    def _ssh_command_add_forward_arguments(self, cmd):
        return cmd

    def _ssh_command_add_extra_arguments(self, cmd):
        return cmd + [
            '-v',
            '-o', 'BatchMode=yes',
            '-o', 'RequestTTY=no',
            '-o', 'ExitOnForwardFailure=yes',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null']

    def _ssh_command_add_destination(self, cmd):
        if self.ssh_port is not None:
            cmd += ['-p', str(self.ssh_port)]
        return cmd + ['@'.join((self.user, self.host))]

    def _ssh_command_add_command(self, cmd):
        return cmd

    def _subprocess_add_args(self, args):
        return args

    @abc.abstractmethod
    def _check_port_forwarding(self):
        pass

    def _kill(self):
        terminator = subproc_utils.ProcTerminator(self.proc)
        terminator()

    def _grab_subprocess_output(self):
        output = 'There is no output from SSH process'
        if self.subprocess_output.tell():
            self.subprocess_output.seek(os.SEEK_SET)
            output = self.subprocess_output.read()
        return output


class SSHLocalPortForwarding(_SSHPortForwardingAbstract):
    def _ssh_command_add_forward_arguments(self, cmd):
        forward = self.bind + self.forward
        forward = [str(x) for x in forward]
        return cmd + ['-L', ':'.join(forward)]

    def _ssh_command_add_extra_arguments(self, cmd):
        parent = super(SSHLocalPortForwarding, self)
        return parent._ssh_command_add_extra_arguments(cmd) + ['-N']

    def _check_port_forwarding(self):
        now = time.time()
        time_end = now + self.validate_timeout
        while now < time_end and self.proc.poll() is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect(self.bind)
            except socket.error:
                time.sleep(1)
                now = time.time()
                continue
            finally:
                s.close()

            break
        else:
            raise exc.SSHForwardedPortValidationError


class SSHRemotePortForwarding(_SSHPortForwardingAbstract):
    def _ssh_command_add_forward_arguments(self, cmd):
        forward = self.bind + self.forward
        forward = [str(x) for x in forward]
        return cmd + ['-R', ':'.join(forward)]

    def _ssh_command_add_extra_arguments(self, cmd):
        parent = super(SSHRemotePortForwarding, self)
        cmd = parent._ssh_command_add_extra_arguments(cmd)
        if not self.validate_timeout:
            cmd += ['-N']
        return cmd

    def _ssh_command_add_command(self, cmd):
        parent = super(SSHRemotePortForwarding, self)
        cmd = parent._ssh_command_add_command(cmd)
        if self.validate_timeout:
            cmd += ['python']
        return cmd

    def _subprocess_add_args(self, args):
        args = super(SSHRemotePortForwarding, self)._subprocess_add_args(args)
        if self.validate_timeout:
            args['stdin'] = subprocess.PIPE
            args['stdout'] = subprocess.PIPE
        return args

    def _check_port_forwarding(self):
        fail_marker = 'CONNECT FAILED'
        success_marker = 'CONNECT APPROVED'
        validate_snippet = textwrap.dedent("""
        import os
        import socket
        import sys
        import time

        timeout = max({timeout}, 1)
        addr = '{address.host}'
        port = {address.port}

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            now = time.time()
            time_end = now + timeout

            while now < time_end:
                try:
                    s.connect((addr, port))
                except socket.error:
                    time.sleep(1)
                    now = time.time()
                    continue
                break
            else:
                sys.stderr.write('{fail_marker}')
                sys.stderr.write(os.linesep)
                sys.exit(1)

            sys.stdout.write('{success_marker}')
            sys.stdout.write(os.linesep)
            sys.stdout.close()
        finally:
            s.close()

        while True:
            time.sleep(128)
        """).lstrip().format(
            address=self.bind, timeout=self.validate_timeout,
            success_marker=success_marker, fail_marker=fail_marker)

        self.proc.stdin.write(validate_snippet)
        self.proc.stdin.close()

        stdout = self.proc.stdout.fileno()
        opts = fcntl.fcntl(stdout, fcntl.F_GETFL)
        fcntl.fcntl(stdout, fcntl.F_SETFL, opts | os.O_NONBLOCK)

        now = time.time()
        time_end = now + self.validate_timeout
        output = []
        while now < time_end:
            ready = select.select([stdout], [], [], time_end - now)
            ready_read = ready[0]
            if ready_read:
                chunk = self.proc.stdout.read(512)
                if not chunk:
                    break
                output.append(chunk)

                if success_marker in ''.join(output):
                    break

            now = time.time()
        else:
            raise exc.SSHForwardedPortValidationError


def forward_remote_port(
        port, user, key_file, target_host, ssh_port=None):
    return SSHRemotePortForwarding(
        user, key_file, target_host,
        NetAddr('127.0.0.1', port), NetAddr('127.0.0.1', port),
        ssh_port=ssh_port)
