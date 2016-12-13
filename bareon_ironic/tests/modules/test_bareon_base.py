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

import fixtures
import mock
from ironic.common import exception
from ironic.conductor import task_manager
from ironic.tests.unit.objects import utils as test_utils

from bareon_ironic.modules import bareon_base
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
