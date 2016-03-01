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

"""An extension for ironic/common/image_service.py"""

import abc
import os
import shutil
import uuid

from oslo_config import cfg
from oslo_concurrency import processutils
from oslo_utils import uuidutils
import requests
import six
import six.moves.urllib.parse as urlparse

from ironic.common import exception
from ironic.common.i18n import _
from ironic.openstack.common import log as logging
from ironic.common import image_service
from ironic.common import keystone
from ironic.common import utils
from ironic.common import swift

from bareon_ironic.modules import bareon_utils

swift_opts = [
    cfg.IntOpt('swift_native_temp_url_duration',
               default=1200,
               help='The length of time in seconds that the temporary URL '
                    'will be valid for. Defaults to 20 minutes. This option '
                    'is different from the "swift_temp_url_duration" defined '
                    'under [glance]. Glance option controls temp urls '
                    'obtained from Glance while this option controls ones '
                    'obtained from Swift directly, e.g. when '
                    'swift:<object> ref is used.')
]


CONF = cfg.CONF
CONF.register_opts(swift_opts, group='swift')

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

IMAGE_CHUNK_SIZE = 1024 * 1024  # 1mb


@six.add_metaclass(abc.ABCMeta)
class BaseImageService(image_service.BaseImageService):
    """Provides retrieval of disk images."""

    def __init__(self, *args, **kwargs):
        super(BaseImageService, self).__init__()

    def get_image_unique_id(self, image_href):
        """Get unique ID of the resource.

        If possible, the ID should change if resource contents are changed.

        :param image_href: Image reference.
        :returns: Unique ID of the resource.
        """
        # NOTE(vdrok): Doing conversion of href in case it's unicode
        # string, UUID cannot be generated for unicode strings on python 2.
        return str(uuid.uuid5(uuid.NAMESPACE_URL,
                              image_href.encode('utf-8')))

    @abc.abstractmethod
    def get_http_href(self, image_href):
        """Get HTTP ref to the image.

        Validate given href and, if possible, convert it to HTTP ref.
        Otherwise raise ImageRefValidationFailed with appropriate message.

        :param image_href: Image reference.
        :raises: exception.ImageRefValidationFailed.
        :returns: http reference to the image
        """


def _GlanceImageService(client=None, version=1, context=None):
    module = image_service.import_versioned_module(version, 'image_service')
    service_class = getattr(module, 'GlanceImageService')
    if (context is not None and CONF.glance.auth_strategy == 'keystone' and
            not context.auth_token):
        context.auth_token = keystone.get_admin_auth_token()
    return service_class(client, version, context)


class GlanceImageService(BaseImageService):
    def __init__(self, client=None, version=1, context=None):
        super(GlanceImageService, self).__init__()
        self.glance = _GlanceImageService(client=client,
                                          version=version,
                                          context=context)

    def __getattr__(self, attr):
        # NOTE(lobur): Known redirects:
        # - swift_temp_url
        return self.glance.__getattribute__(attr)

    def get_image_unique_id(self, image_href):
        return self.validate_href(image_href)['id']

    def get_http_href(self, image_href):
        img_info = self.validate_href(image_href)
        return self.glance.swift_temp_url(img_info)

    def validate_href(self, image_href):
        parsed_ref = urlparse.urlparse(image_href)
        # Supporting both glance:UUID and glance://UUID URLs
        image_href = parsed_ref.path or parsed_ref.netloc
        if not uuidutils.is_uuid_like(image_href):
            images = self.glance.detail(filters={'name': image_href})
            if len(images) == 0:
                raise exception.ImageNotFound(_(
                    'No Glance images found by name %s') % image_href)
            if len(images) > 1:
                raise exception.ImageRefValidationFailed(_(
                    'Multiple Glance images found by name %s') % image_href)
            image_href = images[0]['id']
        return self.glance.show(image_href)

    def download(self, image_href, image_file):
        image_href = self.validate_href(image_href)['id']
        return self.glance.download(image_href, image_file)

    def show(self, image_href):
        return self.validate_href(image_href)


class HttpImageService(image_service.HttpImageService, BaseImageService):
    """Provides retrieval of disk images using HTTP."""

    def get_http_href(self, image_href):
        self.validate_href(image_href)
        return image_href

    def download(self, image_href, image_file):
        """Downloads image to specified location.

        :param image_href: Image reference.
        :param image_file: File object to write data to.
        :raises: exception.ImageRefValidationFailed if GET request returned
            response code not equal to 200.
        :raises: exception.ImageDownloadFailed if:
            * IOError happened during file write;
            * GET request failed.
        """
        try:
            response = requests.get(image_href, stream=True)
            if response.status_code != 200:
                raise exception.ImageRefValidationFailed(
                    image_href=image_href,
                    reason=_(
                        "Got HTTP code %s instead of 200 in response to "
                        "GET request.") % response.status_code)
            response.raw.decode_content = True
            with response.raw as input_img:
                shutil.copyfileobj(input_img, image_file, IMAGE_CHUNK_SIZE)
        except (requests.RequestException, IOError) as e:
            raise exception.ImageDownloadFailed(image_href=image_href,
                                                reason=e)


class FileImageService(image_service.FileImageService, BaseImageService):
    """Provides retrieval of disk images available locally on the conductor."""

    def get_http_href(self, image_href):
        raise exception.ImageRefValidationFailed(
            "File image store is not able to provide HTTP reference.")

    def get_image_unique_id(self, image_href):
        """Get unique ID of the resource.

        :param image_href: Image reference.
        :raises: exception.ImageRefValidationFailed if source image file
            doesn't exist.
        :returns: Unique ID of the resource.
        """
        path = self.validate_href(image_href)
        stat = str(os.stat(path))
        return bareon_utils.md5(stat)


class SwiftImageService(BaseImageService):
    def __init__(self, context):
        self.client = self._get_swiftclient(context)
        super(SwiftImageService, self).__init__()

    def get_image_unique_id(self, image_href):
        return self.show(image_href)['properties']['etag']

    def get_http_href(self, image_href):
        container, object, headers = self.validate_href(image_href)
        return self.client.get_temp_url(
            container, object, CONF.swift.swift_native_temp_url_duration)

    def validate_href(self, image_href):
        path = urlparse.urlparse(image_href).path.lstrip('/')
        if not path:
            raise exception.ImageRefValidationFailed(
                _("No path specified in swift resource reference: %s. "
                  "Reference must be like swift:container/path")
                % str(image_href))

        container, s, object = path.partition('/')
        try:
            headers = self.client.head_object(container, object)
        except exception.SwiftOperationError as e:
            raise exception.ImageRefValidationFailed(
                _("Cannot fetch %(url)s resource. %(exc)s") %
                dict(url=str(image_href), exc=str(e)))

        return (container, object, headers)

    def download(self, image_href, image_file):
        try:
            container, object, headers = self.validate_href(image_href)
            headers, body = self.client.get_object(container, object,
                                                   chunk_size=IMAGE_CHUNK_SIZE)
            for chunk in body:
                image_file.write(chunk)
        except exception.SwiftOperationError as ex:
            raise exception.ImageDownloadFailed(
                _("Cannot fetch %(url)s resource. %(exc)s") %
                dict(url=str(image_href), exc=str(ex)))

    def show(self, image_href):
        container, object, headers = self.validate_href(image_href)
        return {
            'size': int(headers['content-length']),
            'properties': headers
        }

    @staticmethod
    def _get_swiftclient(context):
        return swift.SwiftAPI(user=context.user,
                              preauthtoken=context.auth_token,
                              preauthtenant=context.tenant)


class RsyncImageService(BaseImageService):
    def get_http_href(self, image_href):
        raise exception.ImageRefValidationFailed(
            "Rsync image store is not able to provide HTTP reference.")

    def validate_href(self, image_href):
        path = urlparse.urlparse(image_href).path.lstrip('/')
        if not path:
            raise exception.InvalidParameterValue(
                _("No path specified in rsync resource reference: %s. "
                  "Reference must be like rsync:host::module/path")
                % str(image_href))
        try:
            stdout, stderr = utils.execute(
                'rsync', '--stats', '--dry-run',
                path,
                ".",
                check_exit_code=[0],
                log_errors=processutils.LOG_ALL_ERRORS)
            return path, stdout, stderr

        except (processutils.ProcessExecutionError, OSError) as ex:
            raise exception.ImageRefValidationFailed(
                _("Cannot fetch %(url)s resource. %(exc)s") %
                dict(url=str(image_href), exc=str(ex)))

    def download(self, image_href, image_file):
        path, out, err = self.validate_href(image_href)
        try:
            utils.execute('rsync', '-tvz',
                          path,
                          image_file.name,
                          check_exit_code=[0],
                          log_errors=processutils.LOG_ALL_ERRORS)
        except (processutils.ProcessExecutionError, OSError) as ex:
            raise exception.ImageDownloadFailed(
                _("Cannot fetch %(url)s resource. %(exc)s") %
                dict(url=str(image_href), exc=str(ex)))

    def show(self, image_href):
        path, out, err = self.validate_href(image_href)
        # Example of the size str"
        # "Total file size: 2218131456 bytes"
        size_str = filter(lambda l: "Total file size" in l,
                          out.splitlines())[0]
        size = filter(str.isdigit, size_str.split())[0]
        return {
            'size': int(size),
            'properties': {}
        }


protocol_mapping = {
    'http': HttpImageService,
    'https': HttpImageService,
    'file': FileImageService,
    'glance': GlanceImageService,
    'swift': SwiftImageService,
    'rsync': RsyncImageService,
}


def get_image_service(image_href, client=None, version=1, context=None):
    """Get image service instance to download the image.

    :param image_href: String containing href to get image service for.
    :param client: Glance client to be used for download, used only if
        image_href is Glance href.
    :param version: Version of Glance API to use, used only if image_href is
        Glance href.
    :param context: request context, used only if image_href is Glance href.
    :raises: exception.ImageRefValidationFailed if no image service can
        handle specified href.
    :returns: Instance of an image service class that is able to download
        specified image.
    """
    scheme = urlparse.urlparse(image_href).scheme.lower()
    try:
        cls = protocol_mapping[scheme or 'glance']
    except KeyError:
        raise exception.ImageRefValidationFailed(
            image_href=image_href,
            reason=_('Image download protocol '
                     '%s is not supported.') % scheme
        )

    if cls == GlanceImageService:
        return cls(client, version, context)

    return cls(context)


def _get_glanceclient(context):
    return GlanceImageService(version=2, context=context)


def get_glance_image_uuid_name(task, url):
    """Converting glance links.

    Links like:
        glance:name
        glance://name
        glance:uuid
        glance://uuid
        name
        uuid
    are converted to tuple
        uuid, name

    """
    urlobj = urlparse.urlparse(url)
    if urlobj.scheme and urlobj.scheme != 'glance':
        raise exception.InvalidImageRef("Only glance images are supported.")

    path = urlobj.path or urlobj.netloc
    img_info = _get_glanceclient(task.context).show(path)
    return img_info['id'], img_info['name']
