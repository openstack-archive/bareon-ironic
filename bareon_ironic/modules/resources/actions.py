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

"""
Deploy driver actions.
"""

import datetime
import os
import tempfile

from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log

from ironic.common import exception

from bareon_ironic.modules import bareon_utils
from bareon_ironic.modules.resources import resources
from bareon_ironic.modules.resources import rsync

LOG = log.getLogger(__name__)
CONF = cfg.CONF


# This module allows to run a set of json-defined actions on the node.
#
# Actions json structure:
# {
#     "actions": [
#         {
#             "cmd": "echo 'test 1 run success!'",
#             "name": "test_script_1",
#             "terminate_on_fail": true,
#             "args": "",
#             "sudo": false,
#             "resources": [
#                 {
#                     "name": "swift prefetced",
#                     "mode": "push",
#                     "url": "swift:max_container/swift_pref1",
#                     "target": "/tmp/swift_prefetch_res"
#                 }
#             ]
#         },
#         {
#               another action ...
#         }
#     ]
# }
#
# Each action carries a list of associated resources. Resource, and thus
# action, can be in two states: not_fetched and fetched. See Resource
# documentation. You should always pass a json of not_fetched resources.
#
# The workflow can be one of the following:
# 1. When you need to fetch actions while you have a proper context,
# then serialize them, and run later, deserializing from a file.
# - Create controller from user json -> fetch_action_resources() -> to_dict()
# - Do whatever while actions are serialized, e.g. wait for node boot.
# - Create controller from the serialized json -> execute()
# 2. A workflow when you need to fetch and run actions immediately.
# - Create controller from user json -> execute()


class Action(resources.ResourceList):
    def __init__(self, action, task):
        req = ('name', 'cmd', 'args', 'sudo', 'resources',
               'terminate_on_fail')
        bareon_utils.validate_json(req, action)

        super(Action, self).__init__(action, task)
        LOG.debug("[%s] Action created from %s"
                  % (self._task.node.uuid, action))

    def execute(self, ssh, sftp):
        """Execute action.

        Fetch resources, upload them, and run command.
        """
        cmd = ("%s %s" % (self.cmd, self.args))
        if self.sudo:
            cmd = "sudo %s" % cmd

        self.fetch_resources()
        self.upload_resources(sftp)
        return processutils.ssh_execute(ssh, cmd)

    @staticmethod
    def from_dict(action, task):
        return Action(action, task)


class ActionController(bareon_utils.RawToPropertyMixin):
    def __init__(self, task, action_data):
        self._raw = action_data
        self._task = task
        try:
            req = ('actions',)
            bareon_utils.validate_json(req, action_data)
            self.actions = [Action.from_dict(a, self._task)
                            for a in action_data['actions']]
        except Exception as ex:
            self._save_exception_result(ex)
            raise

    def fetch_action_resources(self):
        """Fetch all resources of all actions.

        Must be idempotent.
        """
        for action in self.actions:
            try:
                action.fetch_resources()
            except Exception as ex:
                # Cleanup is already done in ResourceList.fetch_resources()
                self._save_exception_result(ex)
                raise

    def cleanup_action_resources(self):
        """Cleanup all resources of all actions.

        Must be idempotent.
        Must return None if called when actions resources are not fetched.
        """
        for action in self.actions:
            action.cleanup_resources()

    def _execute(self, ssh, sftp):
        results = []
        # Clean previous results at the beginning
        self._save_results(results)
        for action in self.actions:
            try:
                out, err = action.execute(ssh, sftp)

                results.append({'name': action.name, 'passed': True})
                LOG.info("[%s] Action '%s' finished with:"
                         "\n stdout: %s\n stderr: %s" %
                         (self._task.node.uuid, action.name, out, err))
            except Exception as ex:
                results.append({'name': action.name, 'passed': False,
                                'exception': str(ex)})
                LOG.info("[%s] Action '%s' failed with error: %s" %
                         (self._task.node.uuid, action.name, str(ex)))
                if action.terminate_on_fail:
                    raise
            finally:
                # Save results after each action. Result list will grow until
                # all actions are done.
                self._save_results(results)

    def execute(self, ssh, sftp, **ssh_params):
        """Execute using already opened SSH connection to the node."""
        try:
            if CONF.rsync.rsync_secure_transfer:
                ssh_user = ssh_params.get('username')
                ssh_key_file = ssh_params.get('key_filename')
                ssh_host = ssh_params.get('host')
                ssh_port = ssh_params.get('port', 22)
                with bareon_utils.ssh_tunnel(rsync.RSYNC_PORT, ssh_user,
                                             ssh_key_file, ssh_host, ssh_port):
                    self._execute(ssh, sftp)
            else:
                self._execute(ssh, sftp)

        finally:
            self.cleanup_action_resources()

    def ssh_and_execute(self, node_ip, ssh_user, ssh_key_url):
        """Open an SSH connection to the node and execute."""

        # NOTE(lobur): Security flaw.
        # A random-name tempfile with private key contents exists on Conductor
        # during the time of execution of tenant-image actions when
        # rsync_secure_transfer is True.
        # Because we are using a bash command to start a tunnel we need to
        # have the private key in a file.
        # To fix this we need to start tunnel using Paramiko, which
        # is impossible currently. Paramiko would accept raw key contents,
        # thus we won't need a file.
        with tempfile.NamedTemporaryFile(delete=True) as key_file:
            os.chmod(key_file.name, 0o700)
            try:
                if not (ssh_user and ssh_key_url):
                    raise exception.MissingParameterValue(
                        "Need action_user and action_key params to "
                        "execute actions")

                key_contents = resources.url_download_raw_secured(
                    self._task.context, self._task.node, ssh_key_url)
                key_file.file.write(key_contents)
                key_file.file.flush()

                ssh = bareon_utils.get_ssh_connection(
                    self._task, username=ssh_user,
                    key_contents=key_contents, host=node_ip)
                sftp = ssh.open_sftp()
            except Exception as ex:
                self._save_exception_result(ex)
                raise
            else:
                self.execute(ssh, sftp,
                             username=ssh_user,
                             key_filename=key_file.name,
                             host=node_ip)

    def _save_results(self, results):
        bareon_utils.change_node_dict(
            self._task.node, 'instance_info',
            {'exec_actions': {
                'results': results,
                'finished_at': str(datetime.datetime.utcnow())}})
        self._task.node.save()

    def _save_exception_result(self, ex):
        self._save_results({'exception': str(ex)})
