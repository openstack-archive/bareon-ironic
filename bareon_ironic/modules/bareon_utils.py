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

import copy
import hashlib
import os
import tempfile

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


def node_data_driver(node):
    try:
        driver = node.instance_info['data_driver']
    except KeyError:
        driver = CONF.bareon.agent_data_driver
    return driver


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
