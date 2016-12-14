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
    def setUp(self):
        super(BareonBaseTestCase, self).setUp()

        self.config(enabled_drivers=['bare_swift_ssh'])

        self.temp_dir = self.useFixture(fixtures.TempHomeDir())

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

    @mock.patch.object(bareon_base, 'get_provision_json_path', mock.Mock())
    @mock.patch.object(bareon_utils, 'node_data_driver')
    @mock.patch.object(bareon_base, 'DeploymentConfigValidator')
    def test__validate_deployment_config(self, validator_cls, node_to_data):
        validator_cls.return_value = mock.Mock()
        deploy = bareon_base.BareonDeploy()

        task = mock.Mock()
        for name in ['driver-AAA'] + ['driver-BBB'] * 3 + ['driver-AAA']:
            node_to_data.return_value = name

            deploy._validate_deployment_config(task)

        self.assertEqual(
            [mock.call('driver-AAA'), mock.call('driver-BBB')],
            validator_cls.call_args_list)


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


class DummyError(Exception):
    @property
    def message(self):
        return self.args[0] if self.args else None
