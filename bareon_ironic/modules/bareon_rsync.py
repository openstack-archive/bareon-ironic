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
Bareon Rsync deploy driver.
"""

from oslo_config import cfg

from bareon_ironic.modules import bareon_utils
from bareon_ironic.modules import bareon_base
from bareon_ironic.modules.resources import resources
from bareon_ironic.modules.resources import rsync

rsync_opts = [
    cfg.StrOpt('rsync_master_path',
               default='/rsync/master_images',
               help='Directory where master rsync images are stored on disk.'),
    cfg.IntOpt('image_cache_size',
               default=20480,
               help='Maximum size (in MiB) of cache for master images, '
                    'including those in use.'),
    cfg.IntOpt('image_cache_ttl',
               default=10080,
               help='Maximum TTL (in minutes) for old master images in '
                    'cache.'),
]

CONF = cfg.CONF
CONF.register_opts(rsync_opts, group='rsync')


class BareonRsyncDeploy(bareon_base.BareonDeploy):
    """Interface for deploy-related actions."""

    def _get_deploy_driver(self):
        return 'rsync'

    def _get_image_resource_mode(self):
        return resources.PullMountResource.MODE


class BareonRsyncVendor(bareon_base.BareonVendor):
    def _execute_deploy_script(self, task, ssh, cmd, **kwargs):
        if CONF.rsync.rsync_secure_transfer:
            user = kwargs.get('username', 'root')
            key_file = kwargs.get('key_filename', '/dev/null')
            ssh_port = kwargs.get('bareon_ssh_port', 22)
            host = (kwargs.get('host') or
                    bareon_utils.get_node_ip(kwargs.get('task')))
            with bareon_utils.ssh_tunnel_for_remote_requests(
                    rsync.RSYNC_PORT, user, key_file, host, ssh_port):
                return super(
                    BareonRsyncVendor, self
                )._execute_deploy_script(task, ssh, cmd, **kwargs)
        else:
            return super(
                BareonRsyncVendor, self
            )._execute_deploy_script(task, ssh, cmd, **kwargs)
