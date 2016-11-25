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
import copy
import errno
import fcntl
import hashlib
import os
import select
import socket
import subprocess
import tempfile
import textwrap
import time

import six
from ironic.common import dhcp_factory
from ironic.common import exception
from ironic.common import keystone
from ironic.common import utils
from ironic.common.i18n import _, _LW
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import strutils

from bareon_ironic import exception as exc

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


def get_service_tenant_id():
    ksclient = keystone._get_ksclient()
    if not keystone._is_apiv3(CONF.keystone_authtoken.auth_uri,
                              CONF.keystone_authtoken.auth_version):
        tenant_name = CONF.keystone_authtoken.admin_tenant_name
        if tenant_name:
            return ksclient.tenants.find(name=tenant_name).to_dict()['id']


def change_node_dict(node, dict_name, new_data):
    """Workaround for Ironic object model to update dict."""
    dict_data = getattr(node, dict_name).copy()
    dict_data.update(new_data)
    setattr(node, dict_name, dict_data)


def str_to_alnum(s):
    if not s.isalnum():
        s = ''.join([c for c in s if c.isalnum()])
    return s


def str_replace_non_alnum(s, replace_by="_"):
    if not s.isalnum():
        s = ''.join([(c if c.isalnum() else replace_by) for c in s])
    return s


def validate_json(required, raw):
    for k in required:
        if k not in raw:
            raise exception.MissingParameterValue(
                "%s object missing %s parameter"
                % (str(raw), k)
            )


def get_node_ip(task):
    provider = dhcp_factory.DHCPFactory()
    addresses = provider.provider.get_ip_addresses(task)
    if addresses:
        return addresses[0]
    return None


def get_ssh_connection(task, **kwargs):
    ssh = utils.ssh_connect(kwargs)

    # Note(oberezovskyi): this is required to prevent printing private_key to
    # the conductor log
    if kwargs.get('key_contents'):
        kwargs['key_contents'] = '*****'

    LOG.debug("SSH with params:")
    LOG.debug(kwargs)

    return ssh


def sftp_write_to(sftp, data, path):
    with tempfile.NamedTemporaryFile(dir=CONF.tempdir) as f:
        f.write(data)
        f.flush()
        sftp.put(f.name, path)


def sftp_ensure_tree(sftp, path):
    try:
        sftp.mkdir(path)
    except IOError:
        pass


# TODO(oberezovskyi): merge this code with processutils.ssh_execute
def ssh_execute(ssh, cmd, process_input=None,
                addl_env=None, check_exit_code=True,
                binary=False, timeout=None):
    sanitized_cmd = strutils.mask_password(cmd)
    LOG.debug('Running cmd (SSH): %s', sanitized_cmd)
    if addl_env:
        raise exception.InvalidArgumentError(
            _('Environment not supported over SSH'))

    if process_input:
        # This is (probably) fixable if we need it...
        raise exception.InvalidArgumentError(
            _('process_input not supported over SSH'))

    stdin_stream, stdout_stream, stderr_stream = ssh.exec_command(cmd)
    channel = stdout_stream.channel

    if timeout and not channel.status_event.wait(timeout=timeout):
        raise exception.SSHCommandFailed(cmd=cmd)

    # NOTE(justinsb): This seems suspicious...
    # ...other SSH clients have buffering issues with this approach
    stdout = stdout_stream.read()
    stderr = stderr_stream.read()

    stdin_stream.close()

    exit_status = channel.recv_exit_status()

    if six.PY3:
        # Decode from the locale using using the surrogateescape error handler
        # (decoding cannot fail). Decode even if binary is True because
        # mask_password() requires Unicode on Python 3
        stdout = os.fsdecode(stdout)
        stderr = os.fsdecode(stderr)
    stdout = strutils.mask_password(stdout)
    stderr = strutils.mask_password(stderr)

    # exit_status == -1 if no exit code was returned
    if exit_status != -1:
        LOG.debug('Result was %s' % exit_status)
        if check_exit_code and exit_status != 0:
            raise processutils.ProcessExecutionError(exit_code=exit_status,
                                                     stdout=stdout,
                                                     stderr=stderr,
                                                     cmd=sanitized_cmd)

    if binary:
        if six.PY2:
            # On Python 2, stdout is a bytes string if mask_password() failed
            # to decode it, or an Unicode string otherwise. Encode to the
            # default encoding (ASCII) because mask_password() decodes from
            # the same encoding.
            if isinstance(stdout, unicode):
                stdout = stdout.encode()
            if isinstance(stderr, unicode):
                stderr = stderr.encode()
        else:
            # fsencode() is the reverse operation of fsdecode()
            stdout = os.fsencode(stdout)
            stderr = os.fsencode(stderr)

    return (stdout, stderr)


def umount_without_raise(loc, *args):
    """Helper method to umount without raise."""
    try:
        utils.umount(loc, *args)
    except processutils.ProcessExecutionError as e:
        LOG.warn(_LW("umount_without_raise unable to umount dir %(path)s, "
                     "error: %(e)s"), {'path': loc, 'e': e})


def md5(url):
    """Generate md5 has for the sting."""
    return hashlib.md5(url).hexdigest()


class RawToPropertyMixin(object):
    """A helper mixin for json-based entities.

    Should be used for the json-based class definitions. If you have a class
    corresponding to a json, use this mixin to get direct json <-> class
    attribute mapping. It also gives out-of-the-box serialization back to json.
    """

    _raw = {}

    def __getattr__(self, item):
        if not self._is_special_name(item):
            return self._raw.get(item)

    def __setattr__(self, key, value):
        if (not self._is_special_name(key)) and (key not in self.__dict__):
            self._raw[key] = value
        else:
            self.__dict__[key] = value

    def _is_special_name(self, name):
        return name.startswith("_") or name.startswith("__")

    def to_dict(self):
        data = {}
        for k, v in self._raw.iteritems():
            if (isinstance(v, list) and len(v) > 0 and
                    isinstance(v[0], RawToPropertyMixin)):
                data[k] = [r.to_dict() for r in v]
            else:
                data[k] = v
        return copy.deepcopy(data)


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
        kill_subprocess(self.proc)

    def _grab_subprocess_output(self):
        output = 'There is not any output from SSH process'
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


def ssh_tunnel_for_remote_requests(
        port, user, key_file, target_host, ssh_port=None):
    return SSHRemotePortForwarding(
        user, key_file, target_host,
        NetAddr('127.0.0.1', port), NetAddr('127.0.0.1', port),
        ssh_port=ssh_port)


def kill_subprocess(proc, timeout=10, force_timeout=2, force=True):
    now = time.time()
    try:
        for idx, wait in enumerate((timeout, force_timeout)):
            if not idx:
                proc.terminate()
            else:
                proc.kill()

            time_end = now + wait
            while now < time_end:
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
                now = time.time()
            else:
                if force:
                    continue

            break
        else:
            raise exc.SurvivedSubprocess(pid=proc.pid)
    except OSError as e:
        if e.errno != errno.ESRCH:
            raise
