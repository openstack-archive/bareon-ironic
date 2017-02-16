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

import json

import fixtures
import mock
from ironic.common import exception
from ironic.conductor import task_manager
from ironic.tests.unit.objects import utils as test_utils

from bareon_ironic.modules import bareon_base
from bareon_ironic.modules import bareon_utils
from bareon_ironic.modules.resources import image_service
from bareon_ironic.modules.resources import resources
from bareon_ironic.tests import base


SWIFT_DEPLOY_IMAGE_MODE = resources.PullSwiftTempurlResource.MODE


class BareonBaseTestCase(base.AbstractDBTestCase):
    @mock.patch.object(bareon_base.BareonDeploy,
                       "_get_deploy_driver",
                       mock.Mock(return_value="test_driver"))
    @mock.patch.object(bareon_base.BareonDeploy,
                       "_get_image_resource_mode",
                       mock.Mock(return_value="test_mode"))
    @mock.patch.object(image_service, "get_glance_image_uuid_name",
                       mock.Mock(return_value=('uuid1', 'name1')))
    @mock.patch.object(bareon_base.BareonDeploy, '_fetch_images',
                       mock.Mock())
    def test__add_image_deployment_config__missing_parameter(self):
        origin_image = {
            'name': 'dashes-in-name',
            'url': 'name1',
            'target': '/'
        }

        node = test_utils.create_test_node(
            self.context,
            driver_info={},
            instance_info={'image_source': 'uuid1'})

        for field in ('name', 'url'):
            image = origin_image.copy()
            image.pop(field)
            provision_conf = {'images': [image]}

            with task_manager.acquire(self.context, node.uuid,
                                      driver_name='bare_swift_ssh') as task:
                task.node = node
                deploy = bareon_base.BareonDeploy()
                self.assertRaises(
                    exception.InvalidParameterValue,
                    deploy._add_image_deployment_config, task, provision_conf)

    @mock.patch.object(bareon_base.BareonDeploy,
                       "_get_deploy_driver",
                       mock.Mock(return_value="test_driver"))
    @mock.patch.object(bareon_base.BareonDeploy,
                       "_get_image_resource_mode",
                       mock.Mock(return_value=SWIFT_DEPLOY_IMAGE_MODE))
    @mock.patch.object(image_service, "get_glance_image_uuid_name",
                       mock.Mock(return_value=('uuid1', 'name1')))
    @mock.patch.object(resources.ResourceList, "fetch_resources", mock.Mock())
    def test__add_image_deployment_config_not_alphanum_name(self):
        origin_image = {
            'name': 'dashes-in-name',
            'url': 'name1',
            'target': '/'
        }
        provision_conf = {'images': [origin_image.copy()]}

        node = test_utils.create_test_node(
            self.context,
            driver_info={},
            instance_info={'image_source': 'uuid1'})

        with task_manager.acquire(self.context, node.uuid,
                                  driver_name='bare_swift_ssh') as task:
            task.node = node
            deploy = bareon_base.BareonDeploy()
            with mock.patch.object(
                    bareon_base, 'get_tenant_images_json_path') as path:
                tenant_images_path = self.temp_dir.join('tenant_images.json')
                path.side_effect = lambda node: tenant_images_path

                result = deploy._add_image_deployment_config(
                    task, provision_conf)

        result_image = result['images']
        result_image = result_image[0]

        self.assertEqual(origin_image['name'], result_image['name'])

    @mock.patch.object(bareon_base, 'DeploymentConfigValidator')
    def test__get_deployment_config_validator(self, validator_cls):
        validator_cls.return_value = mock.Mock()
        deploy = bareon_base.BareonDeploy()

        for name in ['driver-AAA'] + ['driver-BBB'] * 3 + ['driver-AAA']:
            deploy._get_deployment_config_validator(name)

        # NOTE(aostapenko) check that no more than one instance of the
        # DeploymentConfigValidator is created for each driver
        self.assertEqual(
            [mock.call('driver-AAA'), mock.call('driver-BBB')],
            validator_cls.call_args_list)

    @mock.patch.object(bareon_base, 'get_provision_json_path')
    @mock.patch.object(bareon_utils, 'node_data_driver')
    @mock.patch.object(bareon_base, 'DeploymentConfigValidator')
    def test__validate_deployment_config(
            self,
            get_deploy_config_validator_mock,
            node_data_driver_mock,
            get_provision_json_path_mock):
        deploy = bareon_base.BareonDeploy()

        driver_name = 'fake_driver_name'
        provision_json = 'fake_provision_json'
        task = mock.Mock()
        validator_mock = mock.Mock()
        get_provision_json_path_mock.return_value = provision_json
        node_data_driver_mock.return_value = driver_name
        get_deploy_config_validator_mock.return_value = validator_mock

        deploy._validate_deployment_config(task)

        node_data_driver_mock.assert_called_once_with(task.node)
        get_deploy_config_validator_mock.assert_called_once_with(driver_name)
        get_provision_json_path_mock.assert_called_once_with(task.node)
        validator_mock.assert_called_once_with(provision_json)


class TestDeploymentConfigValidator(base.AbstractTestCase):
    def setUp(self):
        super(TestDeploymentConfigValidator, self).setUp()

        self.tmpdir = fixtures.TempDir()
        self.useFixture(self.tmpdir)

        self.payload = {
            'ok.json': {'ok': True},
            'fail.json': {'ok': False, 'fail': True}}

        for name in self.payload:
            with open(self.tmpdir.join(name), 'wt') as stream:
                json.dump(self.payload[name], stream)
        with open(self.tmpdir.join('corrupted.json'), 'wt') as stream:
            stream.write('{"corrupted-json-file')

        self.data_driver = mock.Mock()
        self.driver_manager = mock.Mock()
        self.driver_manager.return_value = mock.MagicMock()

        self.extension = mock.Mock()
        self.extension.name = 'dummy'
        self.extension.entry_point.dist.version = str(
            bareon_base.DeploymentConfigValidator._min_version)

        driver_manager_instance = self.driver_manager.return_value
        driver_manager_instance.driver = self.data_driver
        driver_manager_instance.__getitem__.return_value = self.extension

        patch = mock.patch(
            'stevedore.driver.DriverManager', self.driver_manager)
        patch.start()
        self.addCleanup(patch.stop)

    def test_ok(self):
        validator = bareon_base.DeploymentConfigValidator('dummy')
        validator(self.tmpdir.join('ok.json'))

        self.data_driver.validate_data.assert_called_once_with(
            self.payload['ok.json'])

    def test_fail(self):
        self.data_driver.exc.WrongInputDataError = DummyError
        self.data_driver.validate_data.side_effect = DummyError

        validator = bareon_base.DeploymentConfigValidator('dummy')
        self.assertRaises(
            exception.InvalidParameterValue, validator,
            self.tmpdir.join('fail.json'))

        self.data_driver.validate_data.assert_called_once_with(
            self.payload['fail.json'])

    def test_minimal_version(self):
        self.extension.entry_point.dist.version = '0.0.1'

        validator = bareon_base.DeploymentConfigValidator('dummy')
        validator(self.tmpdir.join('ok.json'))

        self.assertEqual(0, self.data_driver.validate_data.call_count)
        self.assertIsNone(validator._driver)

    def test_load_error(self):
        self.driver_manager.side_effect = RuntimeError

        validator = bareon_base.DeploymentConfigValidator('dummy')
        validator(self.tmpdir.join('ok.json'))

        self.assertEqual(None, validator._driver)

    def test_ioerror(self):
        validator = bareon_base.DeploymentConfigValidator('dummy')
        with mock.patch('__builtin__.open') as open_mock:
            open_mock.side_effect = IOError
            self.assertRaises(
                exception.InvalidParameterValue, validator,
                self.tmpdir.join('ok.json'))

    def test_malformed_json(self):
        validator = bareon_base.DeploymentConfigValidator('dummy')
        self.assertRaises(
            exception.InvalidParameterValue, validator,
            self.tmpdir.join('corrupted.json'))


class TestVendorDeployment(base.AbstractDBTestCase):
    temp_dir = None

    ssh_key = ssh_key_pub = node = None
    ssh_key_payload = 'SSH KEY (private)'
    ssh_key_pub_payload = 'SSH KEY (public)'

    def test_deploy_steps_get(self):
        vendor_iface = bareon_base.BareonVendor()
        args = {
            'http_method': 'GET'}

        with task_manager.acquire(
                self.context, self.node.uuid,
                driver_name='bare_swift_ssh') as task:
            step = vendor_iface.deploy_steps(task, **args)

        expected_step = {
            'name': 'inject-ssh-keys',
            'payload': {
                'ssh-keys': {
                    'root': [self.ssh_key_pub_payload]}}}

        self.assertEqual(expected_step, step)

    @mock.patch.object(bareon_base._InjectSSHKeyStepResult, '_handle')
    def test_deploy_steps_post(self, result_handler):
        vendor_iface = bareon_base.BareonVendor()
        args = {
            'http_method': 'POST',
            'name': 'inject-ssh-keys',
            'status': True}

        with task_manager.acquire(
                self.context, self.node.uuid,
                driver_name='bare_swift_ssh') as task:
            step = vendor_iface.deploy_steps(task, **args)

        expected_step = {'url': None}

        self.assertEqual(expected_step, step)
        self.assertEqual(1, result_handler.call_count)

    @mock.patch('ironic.drivers.modules.deploy_utils.set_failed_state')
    def test_deploy_steps_post_fail(self, set_failed_state):
        vendor_iface = bareon_base.BareonVendor()
        args = {
            'http_method': 'POST',
            'name': 'inject-ssh-keys',
            'status': False,
            'status-details': 'Error during step execution.'}

        with task_manager.acquire(
                self.context, self.node.uuid,
                driver_name='bare_swift_ssh') as task:
            step = vendor_iface.deploy_steps(task, **args)

        expected_step = {'url': None}

        self.assertEqual(expected_step, step)
        self.assertEqual(1, set_failed_state.call_count)

    @mock.patch('ironic.drivers.modules.deploy_utils.set_failed_state')
    def test_deploy_steps_post_fail_unbinded(self, set_failed_state):
        vendor_iface = bareon_base.BareonVendor()
        args = {
            'http_method': 'POST',
            'name': None,
            'status': False,
            'status-details': 'Error during step execution.'}

        with task_manager.acquire(
                self.context, self.node.uuid,
                driver_name='bare_swift_ssh') as task:
            step = vendor_iface.deploy_steps(task, **args)

        expected_step = {'url': None}

        self.assertEqual(expected_step, step)
        self.assertEqual(1, set_failed_state.call_count)

    def setUp(self):
        super(TestVendorDeployment, self).setUp()

        self.ssh_key = self.temp_dir.join('bareon-ssh.key')
        self.ssh_key_pub = self.ssh_key + '.pub'

        open(self.ssh_key, 'wt').write('SSH KEY (private)')
        open(self.ssh_key_pub, 'wt').write('SSH KEY (public)')

        self.node = test_utils.create_test_node(
            self.context,
            driver_info={
                'bareon_key_filename': self.ssh_key,
                'bareon_username': 'root'})


class DummyError(Exception):
    @property
    def message(self):
        return self.args[0] if self.args else None
