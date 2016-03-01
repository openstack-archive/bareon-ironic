#
# Copyright 2016 Cray Inc..  All Rights Reserved.
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


import os

from oslo_config import cfg

from ironic.openstack.common import log

rsync_opts = [
    cfg.StrOpt('rsync_server',
               default='$my_ip',
               help='IP address of Ironic compute node\'s rsync server.'),
    cfg.StrOpt('rsync_root',
               default='/rsync',
               help='Ironic compute node\'s rsync root path.'),
    cfg.StrOpt('rsync_module',
               default='ironic_rsync',
               help='Ironic compute node\'s rsync module name.'),
    cfg.BoolOpt('rsync_secure_transfer',
                default=False,
                help='Whether the driver will use secure rsync transfer '
                     'over ssh'),
]

CONF = cfg.CONF
CONF.register_opts(rsync_opts, group='rsync')

LOG = log.getLogger(__name__)

RSYNC_PORT = 873


def get_abs_node_workdir_path(node):
    return os.path.join(CONF.rsync.rsync_root, node.uuid)


def build_rsync_url_from_rel(rel_path, trailing_slash=False):
    if CONF.rsync.rsync_secure_transfer:
        rsync_server_ip = '127.0.0.1'
    else:
        rsync_server_ip = CONF.rsync.rsync_server
    return '%(ip)s::%(mod)s/%(path)s%(sl)s' % dict(
        ip=rsync_server_ip,
        mod=CONF.rsync.rsync_module,
        path=rel_path,
        sl='/' if trailing_slash else ''
    )


def build_rsync_url_from_abs(abs_path, trailing_slash=False):
    rel_path = os.path.relpath(abs_path,
                               CONF.rsync.rsync_root)
    return build_rsync_url_from_rel(rel_path, trailing_slash)
