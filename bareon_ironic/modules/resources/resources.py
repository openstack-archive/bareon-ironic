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


import StringIO
import os

from oslo_config import cfg
from oslo_serialization import jsonutils
from oslo_utils import fileutils
from oslo_log import log
from six.moves.urllib import parse

from ironic.common import exception
from ironic.common import utils
from ironic.common.i18n import _
from ironic.drivers.modules import image_cache

from bareon_ironic.modules import bareon_exception
from bareon_ironic.modules import bareon_utils
from bareon_ironic.modules.resources import rsync
from bareon_ironic.modules.resources import image_service

opts = [
    cfg.StrOpt('default_resource_storage_prefix',
               default='glance:',
               help='A prefix that will be added when resource reference '
                    'is not url-like. E.g. if storage prefix is '
                    '"rsync:10.0.0.10::module1/" then "resource_1" is treated '
                    'like rsync:10.0.0.10::module1/resource_1'),
    cfg.StrOpt('resource_root_path',
               default='/ironic_resources',
               help='Directory where per-node resources are stored.'),
    cfg.StrOpt('resource_cache_master_path',
               default='/ironic_resources/master_resources',
               help='Directory where master resources are stored.'),
    cfg.IntOpt('resource_cache_size',
               default=10240,
               help='Maximum size (in MiB) of cache for master resources, '
                    'including those in use.'),
    cfg.IntOpt('resource_cache_ttl',
               default=1440,
               help='Maximum TTL (in minutes) for old master resources in '
                    'cache.'),
]

CONF = cfg.CONF
CONF.register_opts(opts, group='resources')

LOG = log.getLogger(__name__)


@image_cache.cleanup(priority=25)
class ResourceCache(image_cache.ImageCache):
    def __init__(self, image_service=None):
        super(ResourceCache, self).__init__(
            CONF.resources.resource_cache_master_path,
            # MiB -> B
            CONF.resources.resource_cache_size * 1024 * 1024,
            # min -> sec
            CONF.resources.resource_cache_ttl * 60,
            image_service=image_service)


def get_abs_node_workdir_path(node):
    return os.path.join(CONF.resources.resource_root_path,
                        node.uuid)


def get_node_resources_dir(node):
    return get_abs_node_workdir_path(node)


def get_node_resources_dir_rsync(node):
    return rsync.get_abs_node_workdir_path(node)


def _url_to_filename(url):
    basename = bareon_utils.str_replace_non_alnum(os.path.basename(url))
    return "%(url_hash)s_%(name)s" % dict(url_hash=bareon_utils.md5(url),
                                          name=basename)


def append_storage_prefix(node, url):
    urlobj = parse.urlparse(url)
    if not urlobj.scheme:
        prefix = CONF.resources.default_resource_storage_prefix
        LOG.info('[%(node)s] Plain reference given: "%(ref)s". Adding a '
                 'resource_storage_prefix "%(pref)s".'
                 % dict(node=node.uuid, ref=url, pref=prefix))
        url = prefix + url
    return url


def url_download(context, node, url, dest_path=None):
    if not url:
        return
    url = url.rstrip('/')
    url = append_storage_prefix(node, url)

    LOG.info(_("[%(node)s] Downloading resource by the following url: %(url)s")
             % dict(node=node.uuid, url=url))

    if not dest_path:
        dest_path = os.path.join(get_node_resources_dir(node),
                                 _url_to_filename(url))
    fileutils.ensure_tree(os.path.dirname(dest_path))

    resource_storage = image_service.get_image_service(
        url, context=context)
    # NOTE(lobur): http(s) and rsync resources are cached basing on the URL.
    # They do not have per-revision object UUID / object hash, thus
    # cache cannot identify change of the contents. If URL doesn't change,
    # resource assumed to be the same - cache hit.
    cache = ResourceCache(resource_storage)
    cache.fetch_image(url, dest_path)
    return dest_path


def url_download_raw(context, node, url):
    if not url:
        return
    resource_path = url_download(context, node, url)
    with open(resource_path) as f:
        raw = f.read()
    utils.unlink_without_raise(resource_path)
    return raw


def url_download_json(context, node, url):
    if not url:
        return
    raw = url_download_raw(context, node, url)
    try:
        return jsonutils.loads(raw)
    except ValueError:
        raise exception.InvalidParameterValue(
            _('Resource %s is not a JSON.') % url)


def url_download_raw_secured(context, node, url):
    """Download raw contents of the URL bypassing cache and temp files."""
    if not url:
        return
    url = url.rstrip('/')
    url = append_storage_prefix(node, url)

    scheme = parse.urlparse(url).scheme
    sources_with_tenant_isolation = ('glance', 'swift')
    if scheme not in sources_with_tenant_isolation:
        raise bareon_exception.UnsafeUrlError(
            url=url,
            details="Use one of the following storages "
                    "for this resource: %s"
                    % str(sources_with_tenant_isolation))

    resource_storage = image_service.get_image_service(
        url, context=context)
    out = StringIO.StringIO()
    try:
        resource_storage.download(url, out)
        return out.getvalue()
    finally:
        out.close()


class Resource(bareon_utils.RawToPropertyMixin):
    """Base class for Ironic Resource

    Used to manage a resource which should be fetched from the URL and
    then uploaded to the node. Each resource is a single file.

    Resource can be in two states: not_fetched and fetched. When fetched,
    a resource has an additional attribute, local_path.

    """

    def __init__(self, resource, task, resource_list_name):
        self._raw = resource
        self._resource_list_name = resource_list_name
        self._task = task
        self.name = bareon_utils.str_replace_non_alnum(resource['name'])
        self._validate(resource)

    def _validate(self, raw):
        """Validates resource source json depending of the resource state."""
        if self.is_fetched():
            req = ('name', 'mode', 'target', 'url', 'local_path')
        else:
            req = ('name', 'mode', 'target', 'url')
        bareon_utils.validate_json(req, raw)

    def is_fetched(self):
        """Shows whether the resouce has been fetched or not."""
        return bool(self._raw.get('local_path'))

    def get_dir_names(self):
        """Returns a list of directory names

         These directories show where the resource can reside when serialized.
        """
        raise NotImplemented

    def fetch(self):
        """Download the resource from the URL and store locally.

        Must be idempotent.
        """
        raise NotImplemented

    def upload(self, sftp):
        """Take resource stored locally and put to the node at target path."""
        raise NotImplemented

    def cleanup(self):
        """Cleanup files used to store the resource locally.

        Must be idempotent.
        Must return None if called when resources is not fetched.
        """
        if not self.is_fetched():
            return
        utils.unlink_without_raise(self.local_path)
        self._raw.pop('local_path', None)

    @staticmethod
    def from_dict(resource, task, resource_list_name):
        """Generic method used to instantiate a resource

        Returns particular implementation of the resource depending of the
        'mode' attribute value.
        """
        mode = resource.get('mode')
        if not mode:
            raise exception.InvalidParameterValue(
                "Missing resource 'mode' attribute for %s resource."
                % str(resource)
            )

        mode_to_class = {}
        for c in Resource.__subclasses__():
            mode_to_class[c.MODE] = c

        try:
            return mode_to_class[mode](resource, task, resource_list_name)
        except KeyError:
            raise exception.InvalidParameterValue(
                "Unknown resource mode: '%s'. Supported modes are: "
                "%s " % (mode, str(mode_to_class.keys()))
            )


class PushResource(Resource):
    """A resource with immediate upload

    Resource file is uploaded to the node at target path.
    """

    MODE = 'push'

    def get_dir_names(self):
        my_dir = os.path.join(
            get_node_resources_dir(self._task.node),
            self._resource_list_name
        )
        return [my_dir]

    def fetch(self):
        if self.is_fetched():
            return

        res_dir = self.get_dir_names()[0]
        local_path = os.path.join(res_dir, self.name)
        url_download(self._task.context, self._task.node, self.url,
                     dest_path=local_path)
        self.local_path = local_path

    def upload(self, sftp):
        if not self.is_fetched():
            raise bareon_exception.InvalidResourceState(
                "Cannot upload action '%s' because it is not fetched."
                % self.name
            )
        LOG.info("[%s] Uploading resource %s to the node at %s." % (
            self._task.node.uuid, self.name, self.target))
        bareon_utils.sftp_ensure_tree(sftp, os.path.dirname(self.target))
        sftp.put(self.local_path, self.target)


class PullResource(Resource):
    """A resource with delayed upload

    It is fetched onto rsync share, and rsync pointer is uploaded to the
    node at target path. The user (or action) can use this pointer to download
    the resource from the node shell.
    """

    MODE = 'pull'

    def _validate(self, raw):
        if self.is_fetched():
            req = ('name', 'mode', 'target', 'url', 'local_path',
                   'pull_url')
        else:
            req = ('name', 'mode', 'target', 'url')
        bareon_utils.validate_json(req, raw)

    def get_dir_names(self):
        my_dir = os.path.join(
            get_node_resources_dir_rsync(self._task.node),
            self._resource_list_name
        )
        return [my_dir]

    def fetch(self):
        if self.is_fetched():
            return

        # NOTE(lobur): Security issue.
        # Resources of all tenants are on the same rsync root, so tenant
        # can change the URL manually, remove UUID of the node from path,
        # and rsync whole resource share into it's instance.
        # To solve this we need to create a user per-tenant on Conductor
        # and separate access controls.
        res_dir = self.get_dir_names()[0]
        local_path = os.path.join(res_dir, self.name)
        url_download(self._task.context, self._task.node, self.url,
                     dest_path=local_path)
        pull_url = rsync.build_rsync_url_from_abs(local_path)
        self.local_path = local_path
        self.pull_url = pull_url

    def upload(self, sftp):
        if not self.is_fetched():
            raise bareon_exception.InvalidResourceState(
                "Cannot upload action '%s' because it is not fetched."
                % self.name
            )
        LOG.info("[%(node)s] Writing resource url '%(url)s' to "
                 "the '%(path)s' for further pull."
                 % dict(node=self._task.node.uuid,
                        url=self.pull_url,
                        path=self.target))
        bareon_utils.sftp_ensure_tree(sftp, os.path.dirname(self.target))
        bareon_utils.sftp_write_to(sftp, self.pull_url, self.target)


class PullSwiftTempurlResource(Resource):
    """A resource with delayed upload

    The URL of this resource is converted to Swift temp URL, and written
    to the node at target path. The user (or action) can use this pointer
    to download the resource from the node shell.
    """

    MODE = 'pull-swift-tempurl'

    def _validate(self, raw):
        if self.is_fetched():
            req = ('name', 'mode', 'target', 'url', 'local_path',
                   'pull_url')
        else:
            req = ('name', 'mode', 'target', 'url')
        bareon_utils.validate_json(req, raw)

        url = append_storage_prefix(self._task.node, raw['url'])
        scheme = parse.urlparse(url).scheme
        storages_supporting_tempurl = (
            'glance',
            'swift'
        )
        # NOTE(lobur): Even though we could also use HTTP image service in a
        # manner of tempurl this will not meet user expectation. Swift
        # tempurl in contrast to plain HTTP reference supposed to give
        # scalable access, e.g. allow an image to be pulled from high number
        # of nodes simultaneously without speed degradation.
        if scheme not in storages_supporting_tempurl:
            raise exception.InvalidParameterValue(
                "%(res)s resource can be used only with the "
                "following storages: %(st)s" %
                dict(res=self.__class__.__name__,
                     st=storages_supporting_tempurl)
            )

    def get_dir_names(self):
        res_dir = os.path.join(
            get_node_resources_dir(self._task.node),
            self._resource_list_name
        )
        return [res_dir]

    def fetch(self):
        if self.is_fetched():
            return

        url = append_storage_prefix(self._task.node, self.url)
        # NOTE(lobur): Only Glance and Swift can be here. See _validate.
        img_service = image_service.get_image_service(
            url, version=2, context=self._task.context)
        temp_url = img_service.get_http_href(url)

        res_dir = self.get_dir_names()[0]
        fileutils.ensure_tree(res_dir)
        local_path = os.path.join(res_dir, self.name)
        with open(local_path, 'w') as f:
            f.write(temp_url)

        self.local_path = local_path
        self.pull_url = temp_url

    def upload(self, sftp):
        if not self.is_fetched():
            raise bareon_exception.InvalidResourceState(
                "Cannot upload action '%s' because it is not fetched."
                % self.name
            )
        LOG.info("[%s] Writing %s resource tempurl to the node at %s." % (
            self._task.node.uuid, self.name, self.target))
        bareon_utils.sftp_ensure_tree(sftp, os.path.dirname(self.target))
        sftp.put(self.local_path, self.target)


class PullMountResource(Resource):
    """A resource with delayed upload

    A resource of this type is supposed to be a raw image. It is fetched
    and mounted to the the rsync share. The rsync pointer is uploaded to
    the node at target path. The user (or action) can use this pointer
    to download the resource from the node shell.
    """

    MODE = 'pull-mount'

    def _validate(self, raw):
        if self.is_fetched():
            req = ('name', 'mode', 'target', 'url', 'local_path',
                   'mount_point', 'pull_url')
        else:
            req = ('name', 'mode', 'target', 'url')
        bareon_utils.validate_json(req, raw)

    def get_dir_names(self):
        res_dir = os.path.join(
            get_node_resources_dir(self._task.node),
            self._resource_list_name
        )
        mount_dir = os.path.join(
            get_node_resources_dir_rsync(self._task.node),
            self._resource_list_name,
        )
        return [res_dir, mount_dir]

    def fetch(self):
        if self.is_fetched():
            return
        res_dir, mount_dir = self.get_dir_names()
        local_path = os.path.join(res_dir, self.name)
        url_download(self._task.context, self._task.node, self.url,
                     dest_path=local_path)
        mount_point = os.path.join(mount_dir, self.name)
        fileutils.ensure_tree(mount_point)
        utils.mount(local_path, mount_point, '-o', 'ro')
        pull_url = rsync.build_rsync_url_from_abs(mount_point,
                                                  trailing_slash=True)
        self.local_path = local_path
        self.mount_point = mount_point
        self.pull_url = pull_url

    def upload(self, sftp):
        if not self.is_fetched():
            raise bareon_exception.InvalidResourceState(
                "Cannot upload action '%s' because it is not fetched."
                % self.name
            )
        LOG.info("[%(node)s] Writing resource url '%(url)s' to "
                 "the '%(path)s' for further pull."
                 % dict(node=self._task.node.uuid,
                        url=self.pull_url,
                        path=self.target))
        bareon_utils.sftp_ensure_tree(sftp, os.path.dirname(self.target))
        bareon_utils.sftp_write_to(sftp, self.pull_url, self.target)

    def cleanup(self):
        if not self.is_fetched():
            return
        bareon_utils.umount_without_raise(self.mount_point, '-fl')
        self._raw.pop('mount_point', None)
        utils.unlink_without_raise(self.local_path)
        self._raw.pop('local_path', None)


class ResourceList(bareon_utils.RawToPropertyMixin):
    """Class representing a list of resources."""

    def __init__(self, resource_list, task):
        self._raw = resource_list
        self._task = task

        req = ('name', 'resources')
        bareon_utils.validate_json(req, resource_list)

        self.name = bareon_utils.str_replace_non_alnum(resource_list['name'])
        self.resources = [Resource.from_dict(res, self._task, self.name)
                          for res in resource_list['resources']]

    def fetch_resources(self):
        """Fetch all resources of the list.

        Must be idempotent.
        """
        try:
            for r in self.resources:
                r.fetch()
        except Exception:
            self.cleanup_resources()
            raise

    def upload_resources(self, sftp):
        """Upload all resources of the list."""
        for r in self.resources:
            r.upload(sftp)

    def cleanup_resources(self):
        """Cleanup all resources of the list.

        Must be idempotent.
        Must return None if called when action resources are not fetched.
        """
        # Cleanup resources
        for res in self.resources:
            res.cleanup()

        # Cleanup resource dirs
        res_dirs = []
        for res in self.resources:
            res_dirs.extend(res.get_dir_names())
        for dir in set(res_dirs):
            utils.rmtree_without_raise(dir)

    def __getitem__(self, item):
        return self.resources[item]

    @staticmethod
    def from_dict(resouce_list, task):
        return ResourceList(resouce_list, task)
