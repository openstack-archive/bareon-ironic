#
# Copyright 2015 Mirantis, Inc.
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
Bareon deploy driver.
"""

import json
import os

import eventlet
import six
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_utils import excutils
from oslo_utils import fileutils
from oslo_log import log
from oslo_service import loopingcall

from ironic.common import boot_devices
from ironic.common import dhcp_factory
from ironic.common import exception
from ironic.common import keystone
from ironic.common import pxe_utils
from ironic.common import states
from ironic.common import utils
from ironic.common.glance_service import service_utils
from ironic.common.i18n import _
from ironic.common.i18n import _LE
from ironic.common.i18n import _LI
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.drivers import base
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules import image_cache
from ironic.objects import node as db_node

from bareon_ironic.modules import bareon_exception
from bareon_ironic.modules import bareon_utils
from bareon_ironic.modules.resources import actions
from bareon_ironic.modules.resources import image_service
from bareon_ironic.modules.resources import resources
from bareon_ironic.modules.resources import rsync

agent_opts = [
    cfg.StrOpt('pxe_config_template',
               default=os.path.join(os.path.dirname(__file__),
                                    'bareon_config.template'),
               help='Template file for two-disk boot PXE configuration.'),
    cfg.StrOpt('pxe_config_template_live',
               default=os.path.join(os.path.dirname(__file__),
                                    'bareon_config_live.template'),
               help='Template file for three-disk (live boot) PXE '
                    'configuration.'),
    cfg.StrOpt('bareon_pxe_append_params',
               default='nofb nomodeset vga=normal',
               help='Additional append parameters for baremetal PXE boot.'),
    cfg.StrOpt('deploy_kernel',
               help='UUID (from Glance) of the default deployment kernel.'),
    cfg.StrOpt('deploy_ramdisk',
               help='UUID (from Glance) of the default deployment ramdisk.'),
    cfg.StrOpt('deploy_squashfs',
               help='UUID (from Glance) of the default deployment root FS.'),
    cfg.StrOpt('deploy_config_priority',
               default='instance:node:image:conf',
               help='Priority for deploy config'),
    cfg.StrOpt('deploy_config',
               help='A uuid or name of glance image representing '
                    'deploy config.'),
    cfg.IntOpt('deploy_timeout',
               default=15,
               help="Timeout in minutes for the node continue-deploy process "
                    "(deployment phase following the callback)."),
    cfg.IntOpt('check_terminate_interval',
               help='Time interval in seconds to check whether the deployment '
                    'driver has responded to termination signal',
               default=5),
    cfg.IntOpt('check_terminate_max_retries',
               help='Max retries to check is node already terminated',
               default=20),
]

CONF = cfg.CONF
CONF.register_opts(agent_opts, group='bareon')

LOG = log.getLogger(__name__)

REQUIRED_PROPERTIES = {}
OTHER_PROPERTIES = {
    'deploy_kernel': _('UUID (from Glance) of the deployment kernel.'),
    'deploy_ramdisk': _('UUID (from Glance) of the deployment ramdisk.'),
    'deploy_squashfs': _('UUID (from Glance) of the deployment root FS image '
                         'mounted at boot time.'),
    'bareon_username': _('SSH username; default is "root" Optional.'),
    'bareon_key_filename': _('Name of SSH private key file; default is '
                             '"/etc/ironic/fuel_key". Optional.'),
    'bareon_ssh_port': _('SSH port; default is 22. Optional.'),
    'bareon_deploy_script': _('path to bareon executable entry point; '
                              'default is "provision_ironic" Optional.'),
    'deploy_config': _('Deploy config Glance image id/name'),
}
COMMON_PROPERTIES = OTHER_PROPERTIES

REQUIRED_BAREON_VERSION = "0.0."

TERMINATE_FLAG = 'terminate_deployment'


@image_cache.cleanup(priority=25)
class AgentTFTPImageCache(image_cache.ImageCache):
    def __init__(self, image_service=None):
        super(AgentTFTPImageCache, self).__init__(
            CONF.pxe.tftp_master_path,
            # MiB -> B
            CONF.pxe.image_cache_size * 1024 * 1024,
            # min -> sec
            CONF.pxe.image_cache_ttl * 60,
            image_service=image_service)


def _create_rootfs_link(task):
    """Create Swift temp url for deployment root FS."""
    rootfs = task.node.driver_info['deploy_squashfs']
    if service_utils.is_glance_image(rootfs):
        glance = image_service.GlanceImageService(version=2,
                                                  context=task.context)
        image_info = glance.show(rootfs)
        temp_url = glance.swift_temp_url(image_info)
        temp_url += '&filename=/root.squashfs'
        return temp_url

    try:
        image_service.HttpImageService().validate_href(rootfs)
    except exception.ImageRefValidationFailed:
        with excutils.save_and_reraise_exception():
            LOG.error(_LE("Agent deploy supports only HTTP URLs as "
                          "driver_info['deploy_squashfs']. Either %s "
                          "is not a valid HTTP URL or "
                          "is not reachable."), rootfs)
    return rootfs


def _clean_up_images(task):
    node = task.node
    if node.instance_info.get('images_cleaned_up', False):
        return
    try:
        with open(get_tenant_images_json_path(node)) as f:
            images_json = json.loads(f.read())
    except Exception as ex:
        LOG.warning("Cannot find tenant_images.json for the %s node to"
                    "finish cleanup." % node)
        LOG.warning(str(ex))
    else:
        images = resources.ResourceList.from_dict(images_json, task)
        images.cleanup_resources()
        bareon_utils.change_node_dict(task.node, 'instance_info',
                                      {'images_cleaned_up': True})


class BareonDeploy(base.DeployInterface):
    """Interface for deploy-related actions."""

    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        return COMMON_PROPERTIES

    def validate(self, task):
        """Validate the driver-specific Node deployment info.

        This method validates whether the properties of the supplied node
        contain the required information for this driver to deploy images to
        the node.

        :param task: a TaskManager instance
        :raises: MissingParameterValue
        """
        node = task.node
        params = self._get_boot_files(node)
        error_msg = _('Node %s failed to validate deploy image info. Some '
                      'parameters were missing') % node.uuid
        deploy_utils.check_for_missing_params(params, error_msg)

        self._parse_driver_info(node)

    @task_manager.require_exclusive_lock
    def deploy(self, task):
        """Perform a deployment to a node.

        Perform the necessary work to deploy an image onto the specified node.
        This method will be called after prepare(), which may have already
        performed any preparatory steps, such as pre-caching some data for the
        node.

        :param task: a TaskManager instance.
        :returns: status of the deploy. One of ironic.common.states.
        """
        self._do_pxe_boot(task)
        return states.DEPLOYWAIT

    @task_manager.require_exclusive_lock
    def tear_down(self, task):
        """Tear down a previous deployment on the task's node.

        :param task: a TaskManager instance.
        :returns: status of the deploy. One of ironic.common.states.
        """
        manager_utils.node_power_action(task, states.POWER_OFF)
        return states.DELETED

    def prepare(self, task):
        """Prepare the deployment environment for this node.

        :param task: a TaskManager instance.
        """
        self._fetch_resources(task)
        self._prepare_pxe_boot(task)

    def clean_up(self, task):
        """Clean up the deployment environment for this node.

        If preparation of the deployment environment ahead of time is possible,
        this method should be implemented by the driver. It should erase
        anything cached by the `prepare` method.

        If implemented, this method must be idempotent. It may be called
        multiple times for the same node on the same conductor, and it may be
        called by multiple conductors in parallel. Therefore, it must not
        require an exclusive lock.

        This method is called before `tear_down`.

        :param task: a TaskManager instance.
        """
        # NOTE(lobur): most of the cleanup is done immediately after
        # deployment (see end of pass_deploy_info).
        # - PXE resources are left till the node is unprovisioned because we
        # plan to add support of tenant PXE boot.
        # - Resources such as provision.json are left till the node is
        # unprovisioned to simplify debugging.
        self._clean_up_pxe(task)
        _clean_up_images(task)
        self._clean_up_resource_dirs(task)

    def take_over(self, task):
        pass

    def _clean_up_pxe(self, task):
        """Clean up left over PXE and DHCP files."""
        pxe_info = self._get_tftp_image_info(task.node)
        for label in pxe_info:
            path = pxe_info[label][1]
            utils.unlink_without_raise(path)
        AgentTFTPImageCache().clean_up()
        pxe_utils.clean_up_pxe_config(task)

    def _fetch_resources(self, task):
        self._fetch_provision_json(task)
        self._fetch_actions(task)

    def _fetch_provision_json(self, task):
        config = self._get_deploy_config(task)
        config = self._add_image_deployment_config(task, config)

        deploy_data = config.get('deploy_data', {})
        if 'kernel_params' not in deploy_data:
            deploy_data['kernel_params'] = CONF.bareon.bareon_pxe_append_params
        config['deploy_data'] = deploy_data

        LOG.info('[{0}] Resulting provision.json is:\n{1}'.format(
            task.node.uuid, config))

        # On fail script is not passed to the agent, it is handled on
        # Conductor.
        on_fail_script_url = config.pop("on_fail_script", None)
        self._fetch_on_fail_script(task, on_fail_script_url)

        filename = get_provision_json_path(task.node)
        LOG.info('[{0}] Writing provision.json to:\n{1}'.format(
            task.node.uuid, filename))
        with open(filename, 'w') as f:
            f.write(json.dumps(config))

    def _get_deploy_config(self, task):
        node = task.node
        instance_info = node.instance_info

        # Get options passed by nova, if any.
        deploy_config_options = instance_info.get('deploy_config_options', {})
        # Get options available at ironic side.
        deploy_config_options['node'] = node.driver_info.get('deploy_config')
        deploy_config_options['conf'] = CONF.bareon.deploy_config
        # Cleaning empty options.
        deploy_config_options = {k: v for k, v in
                                 six.iteritems(deploy_config_options) if v}

        configs = self._fetch_deploy_configs(task.context, node,
                                             deploy_config_options)
        return self._merge_configs(configs)

    def _fetch_deploy_configs(self, context, node, cfg_options):
        configs = {}
        for key, url in six.iteritems(cfg_options):
            configs[key] = resources.url_download_json(context, node,
                                                       url)
        return configs

    @staticmethod
    def _merge_configs(configs):
        # Merging first level attributes of configs according to priority
        priority_list = CONF.bareon.deploy_config_priority.split(':')
        unknown_sources = set(priority_list) - {'instance', 'node', 'conf',
                                                'image'}
        if unknown_sources:
            raise ValueError('Unknown deploy config source %s' % str(
                unknown_sources))

        result = {}
        for k in priority_list[::-1]:
            if k in configs:
                result.update(configs[k])
        LOG.debug('Resulting deploy config:')
        LOG.debug('%s', result)
        return result

    def _fetch_on_fail_script(self, task, url):
        if not url:
            return
        path = get_on_fail_script_path(task.node)
        LOG.info('[{0}] Fetching on_fail_script to:\n{1}'.format(
            task.node.uuid, path))
        resources.url_download(task.context, task.node, url, path)

    def _fetch_actions(self, task):
        driver_actions_url = task.node.instance_info.get('driver_actions')
        actions_data = resources.url_download_json(task.context,
                                                   task.node,
                                                   driver_actions_url)
        if not actions_data:
            LOG.info("[%s] No driver_actions specified" % task.node.uuid)
            return

        controller = actions.ActionController(task, actions_data)
        controller.fetch_action_resources()

        actions_data = controller.to_dict()
        LOG.info('[{0}] Deploy actions for the node are:\n{1}'.format(
            task.node.uuid, actions_data))

        filename = get_actions_json_path(task.node)
        LOG.info('[{0}] Writing actions.json to:\n{1}'.format(
            task.node.uuid, filename))
        with open(filename, 'w') as f:
            f.write(json.dumps(actions_data))

    def _clean_up_resource_dirs(self, task):
        utils.rmtree_without_raise(
            resources.get_abs_node_workdir_path(task.node))
        utils.rmtree_without_raise(
            rsync.get_abs_node_workdir_path(task.node))

    def _build_instance_info_for_deploy(self, task):
        raise NotImplementedError

    def _do_pxe_boot(self, task, ports=None):
        """Reboot the node into the PXE ramdisk.

        :param task: a TaskManager instance
        :param ports: a list of Neutron port dicts to update DHCP options on.
            If None, will get the list of ports from the Ironic port objects.
        """
        dhcp_opts = pxe_utils.dhcp_options_for_instance(task)
        provider = dhcp_factory.DHCPFactory()
        provider.update_dhcp(task, dhcp_opts, ports)
        manager_utils.node_set_boot_device(task, boot_devices.PXE,
                                           persistent=True)
        manager_utils.node_power_action(task, states.REBOOT)

    def _cache_tftp_images(self, ctx, node, pxe_info):
        """Fetch the necessary kernels and ramdisks for the instance."""
        fileutils.ensure_tree(
            os.path.join(CONF.pxe.tftp_root, node.uuid))
        LOG.debug("Fetching kernel and ramdisk for node %s",
                  node.uuid)
        deploy_utils.fetch_images(ctx, AgentTFTPImageCache(),
                                  pxe_info.values())

    def _prepare_pxe_boot(self, task):
        """Prepare the files required for PXE booting the agent."""
        pxe_info = self._get_tftp_image_info(task.node)

        # Do live boot if squashfs is specified in either way.
        is_live_boot = (task.node.driver_info.get('deploy_squashfs') or
                        CONF.bareon.deploy_squashfs)
        pxe_options = self._build_pxe_config_options(task, pxe_info,
                                                     is_live_boot)
        template = (CONF.bareon.pxe_config_template_live if is_live_boot
                    else CONF.bareon.pxe_config_template)
        pxe_utils.create_pxe_config(task,
                                    pxe_options,
                                    template)

        self._cache_tftp_images(task.context, task.node, pxe_info)

    def _get_tftp_image_info(self, node):
        params = self._get_boot_files(node)
        return pxe_utils.get_deploy_kr_info(node.uuid, params)

    def _build_pxe_config_options(self, task, pxe_info, live_boot):
        """Builds the pxe config options for booting agent.

        This method builds the config options to be replaced on
        the agent pxe config template.

        :param task: a TaskManager instance
        :param pxe_info: A dict containing the 'deploy_kernel' and
            'deploy_ramdisk' for the agent pxe config template.
        :returns: a dict containing the options to be applied on
        the agent pxe config template.
        """
        ironic_api = (CONF.conductor.api_url or
                      keystone.get_service_url()).rstrip('/')

        agent_config_opts = {
            'deployment_aki_path': pxe_info['deploy_kernel'][1],
            'deployment_ari_path': pxe_info['deploy_ramdisk'][1],
            'bareon_pxe_append_params': CONF.bareon.bareon_pxe_append_params,
            'deployment_id': task.node.uuid,
            'api-url': ironic_api,
        }

        if live_boot:
            agent_config_opts['rootfs-url'] = _create_rootfs_link(task)

        return agent_config_opts

    def _get_boot_files(self, node):
        d_info = node.driver_info
        params = {
            'deploy_kernel': d_info.get('deploy_kernel',
                                        CONF.bareon.deploy_kernel),
            'deploy_ramdisk': d_info.get('deploy_ramdisk',
                                         CONF.bareon.deploy_ramdisk),
        }
        # Present only when live boot is used
        squashfs = d_info.get('deploy_squashfs', CONF.bareon.deploy_squashfs)
        if squashfs:
            params['deploy_squashfs'] = squashfs

        return params

    def _get_image_resource_mode(self):
        raise NotImplementedError

    def _get_deploy_driver(self):
        raise NotImplementedError

    def _add_image_deployment_config(self, task, provision_config):
        node = task.node
        bareon_utils.change_node_dict(
            node, 'instance_info',
            {'deploy_driver': self._get_deploy_driver()})
        node.save()

        image_resource_mode = self._get_image_resource_mode()
        boot_image = node.instance_info['image_source']
        default_user_images = [
            {
                'name': boot_image,
                'url': boot_image,
                'target': "/",
            }
        ]
        user_images = provision_config.get('images', default_user_images)

        for image in user_images:
            image_uuid, image_name = image_service.get_glance_image_uuid_name(
                task, image['url'])
            image['boot'] = (boot_image == image_uuid)
            image['url'] = "glance:%s" % image_uuid
            image['mode'] = image_resource_mode
            image['image_uuid'] = image_uuid
            image['image_name'] = image_name

        fetched_image_resources = self._fetch_images(task, user_images)

        image_deployment_config = [
            {
                'name': image.name,
                'image_pull_url': image.pull_url,
                'target': image.target,
                'boot': image.boot,
                'image_uuid': image.image_uuid,
                'image_name': image.image_name
            }
            for image in fetched_image_resources
        ]

        bareon_utils.change_node_dict(
            task.node, 'instance_info',
            {'multiboot': len(image_deployment_config) > 1})
        node.save()

        provision_config['images'] = image_deployment_config
        return provision_config

    def _fetch_images(self, task, image_resources):
        images = resources.ResourceList({
            "name": "tenant_images",
            "resources": image_resources
        }, task)
        images.fetch_resources()

        # NOTE(lobur): serialize tenant images json for further cleanup.
        images_json = images.to_dict()
        with open(get_tenant_images_json_path(task.node), 'w') as f:
            f.write(json.dumps(images_json))

        return images.resources

    @staticmethod
    def _parse_driver_info(node):
        """Gets the information needed for accessing the node.

        :param node: the Node object.
        :returns: dictionary of information.
        :raises: InvalidParameterValue if any required parameters are
            incorrect.
        :raises: MissingParameterValue if any required parameters are missing.

        """
        info = node.driver_info
        d_info = {}
        error_msgs = []

        d_info['username'] = info.get('bareon_username', 'root')
        d_info['key_filename'] = info.get('bareon_key_filename',
                                          '/etc/ironic/fuel_key')

        if not os.path.isfile(d_info['key_filename']):
            error_msgs.append(_("SSH key file %s not found.") %
                              d_info['key_filename'])

        try:
            d_info['port'] = int(info.get('bareon_ssh_port', 22))
        except ValueError:
            error_msgs.append(_("'bareon_ssh_port' must be an integer."))

        if error_msgs:
            msg = (_('The following errors were encountered while parsing '
                     'driver_info:\n%s') % '\n'.join(error_msgs))
            raise exception.InvalidParameterValue(msg)

        d_info['script'] = info.get('bareon_deploy_script', 'bareon-provision')

        return d_info

    def terminate_deployment(self, task):
        node = task.node
        if TERMINATE_FLAG not in node.instance_info:

            def _wait_for_node_to_become_terminated(retries, max_retries,
                                                    task):
                task_node = task.node
                retries[0] += 1
                if retries[0] > max_retries:
                    bareon_utils.change_node_dict(
                        task_node, 'instance_info',
                        {TERMINATE_FLAG: 'failed'})
                    task_node.reservation = None
                    task_node.save()

                    raise bareon_exception.RetriesException(
                        retry_count=max_retries)

                current_node = db_node.Node.get_by_uuid(task.context,
                                                        task_node.uuid)
                if current_node.instance_info.get(TERMINATE_FLAG) == 'done':
                    raise loopingcall.LoopingCallDone()

            bareon_utils.change_node_dict(
                node, 'instance_info',
                {TERMINATE_FLAG: 'requested'})
            node.save()

            retries = [0]
            interval = CONF.bareon.check_terminate_interval
            max_retries = CONF.bareon.check_terminate_max_retries

            timer = loopingcall.FixedIntervalLoopingCall(
                _wait_for_node_to_become_terminated,
                retries, max_retries, task)
            try:
                timer.start(interval=interval).wait()
            except bareon_exception.RetriesException as ex:
                LOG.error('Failed to terminate node. Error: %(error)s' % {
                    'error': ex})

    @property
    def can_terminate_deployment(self):
        return True


class BareonVendor(base.VendorInterface):
    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        return COMMON_PROPERTIES

    def validate(self, task, method, **kwargs):
        """Validate the driver-specific Node deployment info.

        :param task: a TaskManager instance
        :param method: method to be validated
        """
        if method == 'exec_actions':
            return

        if method == 'switch_boot':
            self.validate_switch_boot(task, **kwargs)
            return

        if not kwargs.get('status'):
            raise exception.MissingParameterValue(_('Unknown Bareon status'
                                                    ' on a node.'))
        if not kwargs.get('address'):
            raise exception.MissingParameterValue(_('Bareon must pass '
                                                    'address of a node.'))
        BareonDeploy._parse_driver_info(task.node)

    def validate_switch_boot(self, task, **kwargs):
        if not kwargs.get('image'):
            raise exception.MissingParameterValue(_('No image info passed.'))
        if not kwargs.get('ssh_key'):
            raise exception.MissingParameterValue(_('No ssh key info passed.'))
        if not kwargs.get('ssh_user'):
            raise exception.MissingParameterValue(_('No ssh user info '
                                                    'passed.'))

    @base.passthru(['POST'])
    @task_manager.require_exclusive_lock
    def pass_deploy_info(self, task, **kwargs):
        """Continues the deployment of baremetal node."""
        node = task.node
        task.process_event('resume')
        err_msg = _('Failed to continue deployment with Bareon.')

        agent_status = kwargs.get('status')
        if agent_status != 'ready':
            LOG.error(_LE('Deploy failed for node %(node)s. Bareon is not '
                          'in ready state, error: %(error)s'),
                      {'node': node.uuid,
                       'error': kwargs.get('error_message')})
            deploy_utils.set_failed_state(task, err_msg)
            return

        params = BareonDeploy._parse_driver_info(node)
        params['host'] = kwargs.get('address')

        cmd = '%s --data_driver ironic --deploy_driver %s' % (
            params.pop('script'), node.instance_info['deploy_driver'])
        if CONF.debug:
            cmd += ' --debug'
        instance_info = node.instance_info

        try:
            ssh = bareon_utils.get_ssh_connection(task, **params)
            sftp = ssh.open_sftp()

            self._check_bareon_version(ssh, node.uuid)

            provision_config_path = get_provision_json_path(task.node)
            # TODO(yuriyz) no hardcode
            sftp.put(provision_config_path, '/tmp/provision.json')

            # swift configdrive store should be disabled
            configdrive = instance_info.get('configdrive')
            if configdrive is not None:
                # TODO(yuriyz) no hardcode
                bareon_utils.sftp_write_to(sftp, configdrive,
                                           '/tmp/config-drive.img')

            out, err = self._deploy(task, ssh, cmd, **params)
            LOG.info(_LI('[%(node)s] Bareon pass on node %(node)s'),
                     {'node': node.uuid})
            LOG.debug('[%s] Bareon stdout is: "%s"', node.uuid, out)
            LOG.debug('[%s] Bareon stderr is: "%s"', node.uuid, err)

            self._get_boot_info(task, ssh)

            self._run_actions(task, ssh, sftp, params)

            manager_utils.node_power_action(task, states.POWER_OFF)
            manager_utils.node_set_boot_device(task, boot_devices.DISK,
                                               persistent=True)
            manager_utils.node_power_action(task, states.POWER_ON)

        except exception.SSHConnectFailed as e:
            msg = (
                _('[%(node)s] SSH connect to node %(host)s failed. '
                  'Error: %(error)s') % {'host': params['host'], 'error': e,
                                         'node': node.uuid})
            self._deploy_failed(task, msg)

        except exception.ConfigInvalid as e:
            msg = (_('[%(node)s] Invalid provision config. '
                     'Error: %(error)s') % {'error': e, 'node': node.uuid})
            self._deploy_failed(task, msg)

        except bareon_exception.DeployTerminationSucceed:
            LOG.info(_LI('[%(node)s] Deployment was terminated'),
                     {'node': node.uuid})

        except Exception as e:
            self._run_on_fail_script(task, sftp, ssh)

            msg = (_('[%(node)s] Deploy failed for node %(node)s. '
                     'Error: %(error)s') % {'node': node.uuid, 'error': e})
            self._bareon_log(task, ssh)
            self._deploy_failed(task, msg)

        else:
            task.process_event('done')
            LOG.info(_LI('Deployment to node %s done'), task.node.uuid)

        finally:
            self._clean_up_deployment_resources(task)

    def _deploy_failed(self, task, msg):
        LOG.error(msg)
        deploy_utils.set_failed_state(task, msg)

    def _check_bareon_version(self, ssh, node_uuid):
        try:
            stdout, stderr = processutils.ssh_execute(
                ssh, 'cat /etc/bareon-release')

            LOG.info(_LI("[{0}] Tracing Bareon version.\n{1}").format(
                node_uuid, stdout))

            version = ""
            lines = stdout.splitlines()
            if lines:
                version_line = lines[0]
                name, _, version = version_line.partition("==")
                if version.startswith(REQUIRED_BAREON_VERSION):
                    return

            msg = ("Bareon version '%(req)s' is required, but version "
                   "'%(found)s' found on the ramdisk."
                   % dict(req=REQUIRED_BAREON_VERSION,
                          found=version))
            raise bareon_exception.IncompatibleRamdiskVersion(details=msg)
        except processutils.ProcessExecutionError:
            msg = "Bareon version cannot be read on the ramdisk."
            raise bareon_exception.IncompatibleRamdiskVersion(details=msg)

    def _get_boot_info(self, task, ssh):
        node = task.node
        node_uuid = node.uuid

        if not node.instance_info.get('multiboot', False):
            return
        try:
            stdout, stderr = processutils.ssh_execute(
                ssh, 'cat /tmp/boot_entries.json')
        except processutils.ProcessExecutionError as exec_err:
            LOG.warning(_LI('[%(node)s] Error getting boot info. '
                            'Error: %(error)s') % {'node': node_uuid,
                                                   'error': exec_err})
            raise
        else:
            multiboot_info = json.loads(stdout)
            bareon_utils.change_node_dict(node, 'instance_info', {
                'multiboot_info': multiboot_info
            })
            LOG.info("[{1}] {0} Multiboot info {0}\n{2}"
                     "\n".format("#" * 20, node_uuid, multiboot_info))

    def _run_actions(self, task, ssh, sftp, sshparams):
        actions_path = get_actions_json_path(task.node)
        if not os.path.exists(actions_path):
            LOG.info(_LI("[%(node)s] No actions specified. Skipping")
                     % {'node': task.node.uuid})
            return

        with open(actions_path) as f:
            actions_data = json.loads(f.read())
        actions_controller = actions.ActionController(
            task, actions_data
        )

        actions_controller.execute(ssh, sftp, **sshparams)

    def _bareon_log(self, task, ssh):
        node_uuid = task.node.uuid
        try:
            # TODO(oberezovskyi): Chenge log pulling mechanism (e.g. use
            # remote logging feature of syslog)
            stdout, stderr = processutils.ssh_execute(
                ssh, 'cat /var/log/bareon.log')
        except processutils.ProcessExecutionError as exec_err:
            LOG.warning(_LI('[%(node)s] Error getting Bareon log. '
                            'Error: %(error)s') % {'node': node_uuid,
                                                   'error': exec_err})
        else:
            LOG.info("[{1}] {0} Start Bareon log {0}\n{2}\n"
                     "[{1}] {0} End Bareon log {0}".format("#" * 20,
                                                           node_uuid,
                                                           stdout))

    def _run_on_fail_script(self, task, sftp, ssh):
        node = task.node
        node_uuid = node.uuid
        try:
            on_fail_script_path = get_on_fail_script_path(node)
            if not os.path.exists(on_fail_script_path):
                LOG.info(_LI("[%(node)s] No on_fail_script passed. Skipping")
                         % {'node': node_uuid})
                return

            LOG.debug(_LI('[%(node)s] Uploading on_fail script to the node.'),
                      {'node': node_uuid})
            sftp.put(on_fail_script_path, '/tmp/bareon_on_fail.sh')

            LOG.debug("[%(node)s] Executing on_fail_script."
                      % {'node': node_uuid})
            out, err = processutils.ssh_execute(
                ssh, "bash %s" % '/tmp/bareon_on_fail.sh')

        except processutils.ProcessExecutionError as ex:
            LOG.warning(_LI('[%(node)s] Error executing OnFail script. '
                            'Error: %(er)s') % {'node': node_uuid, 'er': ex})

        except exception.SSHConnectFailed as ex:
            LOG.warning(_LI('[%(node)s] SSH connection error. '
                            'Error: %(er)s') % {'node': node_uuid, 'er': ex})

        except Exception as ex:
            LOG.warning(_LI('[%(node)s] Unknown error. '
                            'Error: %(error)s') % {'node': node_uuid,
                                                   'error': ex})
        else:
            LOG.info(
                "{0} [{1}] on_fail sctipt result below {0}".format("#" * 40,
                                                                   node_uuid))
            LOG.info(out)
            LOG.info(err)
            LOG.info("{0} [{1}] End on_fail script "
                     "result {0}".format("#" * 40, node_uuid))

    def _clean_up_deployment_resources(self, task):
        _clean_up_images(task)
        self._clean_up_actions(task)

    def _clean_up_actions(self, task):
        filename = get_actions_json_path(task.node)
        if not os.path.exists(filename):
            return

        with open(filename) as f:
            actions_data = json.loads(f.read())

        controller = actions.ActionController(task, actions_data)
        controller.cleanup_action_resources()

    @base.passthru(['POST'])
    @task_manager.require_exclusive_lock
    def exec_actions(self, task, **kwargs):
        actions_json = resources.url_download_json(
            task.context, task.node, kwargs.get('driver_actions'))
        if not actions_json:
            LOG.info("[%s] No driver_actions specified." % task.node.uuid)
            return

        ssh_user = actions_json.pop('action_user')
        ssh_key_url = actions_json.pop('action_key')
        node_ip = bareon_utils.get_node_ip(task)

        controller = actions.ActionController(task, actions_json)
        controller.ssh_and_execute(node_ip, ssh_user, ssh_key_url)

    def _execute_deploy_script(self, task, ssh, cmd, *args, **kwargs):
        # NOTE(oberezovskyi): minutes to seconds
        timeout = CONF.bareon.deploy_timeout * 60
        LOG.debug('[%s] Running cmd (SSH): %s', task.node.uuid, cmd)
        try:
            out, err = bareon_utils.ssh_execute(ssh, cmd, timeout=timeout,
                                                check_exit_code=True)
        except exception.SSHCommandFailed as err:
            LOG.debug('[%s] Deploy script execute failed: "%s"',
                      task.node.uuid, err)
            raise bareon_exception.DeploymentTimeout(timeout=timeout)
        return out, err

    def _deploy(self, task, ssh, cmd, **params):
        deployment_thread = eventlet.spawn(self._execute_deploy_script,
                                           task, ssh, cmd, **params)

        def _wait_for_deployment_finished(task, thread):
            task_node = task.node
            current_node = db_node.Node.get_by_uuid(task.context,
                                                    task_node.uuid)

            # NOTE(oberezovskyi): greenthread have no way to check is
            # thread already finished, so need to access to
            # private variable
            if thread._exit_event.ready():
                raise loopingcall.LoopingCallDone()

            if (current_node.instance_info.get(TERMINATE_FLAG,
                                               '') == 'requested'):
                thread.kill()
                bareon_utils.change_node_dict(
                    task_node, 'instance_info',
                    {TERMINATE_FLAG: 'done'})
                task_node.save()
                raise bareon_exception.DeployTerminationSucceed()

        timer = loopingcall.FixedIntervalLoopingCall(
            _wait_for_deployment_finished, task, deployment_thread)
        timer.start(interval=5).wait()
        return deployment_thread.wait()

    @base.passthru(['POST'], async=False)
    @task_manager.require_exclusive_lock
    def switch_boot(self, task, **kwargs):
        # NOTE(oberezovskyi): exception messages should not be changed because
        # of hardcode in nova-ironic driver
        image = kwargs.get('image')
        LOG.info('[{0}] Attempt to switch boot to {1} '
                 'image'.format(task.node.uuid, image))

        msg = ""
        try:
            if not task.node.instance_info.get('multiboot', False):
                msg = "[{}] Non-multiboot deployment".format(task.node.uuid)
                raise exception.IronicException(message=msg, code=400)

            boot_info = task.node.instance_info.get('multiboot_info',
                                                    {'elements': []})

            grub_id = next((element['grub_id']
                            for element in boot_info['elements']
                            if (element['image_uuid'] == image or
                                element['image_name'] == image)), None)

            if grub_id is None:
                msg = ('[{}] Can\'t find desired multiboot '
                       'image'.format(task.node.uuid))
                raise exception.IronicException(message=msg, code=400)

            elif grub_id == boot_info.get('current_element', None):
                msg = ('[{}] Already in desired boot '
                       'device.'.format(task.node.uuid))
                raise exception.IronicException(message=msg, code=400)

            node_ip = bareon_utils.get_node_ip(task)
            ssh_key = resources.url_download_raw_secured(task.context,
                                                         task.node,
                                                         kwargs['ssh_key'])
            ssh = bareon_utils.get_ssh_connection(task, **{
                'host': node_ip,
                'username': kwargs['ssh_user'],
                'key_contents': ssh_key
            })

            tmp_path = processutils.ssh_execute(ssh, 'mktemp -d')[0].split()[0]
            cfg_path = os.path.join(tmp_path, 'boot', 'grub2', 'grub.cfg')

            commands = [
                'mount /dev/disk/by-uuid/{} {}'.format(
                    boot_info['multiboot_partition'],
                    tmp_path),
                "sed -i 's/\(set default=\)[0-9]*/\\1{}/' {}".format(grub_id,
                                                                     cfg_path),
                'umount {}'.format(tmp_path),
                'rmdir {}'.format(tmp_path)
            ]

            map(lambda cmd: processutils.ssh_execute(ssh, 'sudo ' + cmd),
                commands)

        except exception.SSHConnectFailed as e:
            msg = (
                _('[%(node)s] SSH connect to node %(host)s failed. '
                  'Error: %(error)s') % {'host': node_ip, 'error': e,
                                         'node': task.node.uuid})
            raise exception.IronicException(message=msg, code=400)

        except exception.IronicException as e:
            msg = str(e)
            raise

        except Exception as e:
            msg = (_('[%(node)s] Multiboot switch failed for node %(node)s. '
                     'Error: %(error)s') % {'node': task.node.uuid,
                                            'error': e})
            raise exception.IronicException(message=msg, code=400)

        else:
            boot_info['current_element'] = grub_id
            bareon_utils.change_node_dict(
                task.node, 'instance_info',
                {'multiboot_info': boot_info})
            task.node.save()

        finally:
            if msg:
                LOG.error(msg)
                task.node.last_error = msg
                task.node.save()


def get_provision_json_path(node):
    return os.path.join(resources.get_node_resources_dir(node),
                        "provision.json")


def get_actions_json_path(node):
    return os.path.join(resources.get_node_resources_dir(node),
                        "actions.json")


def get_on_fail_script_path(node):
    return os.path.join(resources.get_node_resources_dir(node),
                        "on_fail_script.sh")


def get_tenant_images_json_path(node):
    return os.path.join(resources.get_node_resources_dir(node),
                        "tenant_images.json")
