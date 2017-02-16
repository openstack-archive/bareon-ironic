#
# Copyright 2016 Cray Inc., All Rights Reserved
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Bareon driver exceptions"""

import pprint

from ironic.common import exception
from ironic.common.i18n import _


class IncompatibleRamdiskVersion(exception.IronicException):
    message = _("Incompatible node ramdisk version. %(details)s")


class UnsafeUrlError(exception.IronicException):
    message = _("URL '%(url)s' is not safe and cannot be used for sensitive "
                "data. %(details)s")


class InvalidResourceState(exception.IronicException):
    pass


class DeploymentTimeout(exception.IronicException):
    message = _("Deployment timeout expired. Timeout: %(timeout)s")


class RetriesException(exception.IronicException):
    message = _("Retries count exceeded. Retried %(retry_count)d times.")


class DeployTerminationSucceed(exception.IronicException):
    message = _("Deploy termination succeed.")


class BootSwitchFailed(exception.IronicException):
    message = _("Boot switch failed. Error: %(error)s")


class DeployProtocolError(exception.IronicException):
    _msg_fmt = _('Corrupted deploy protocol message: %(details)s\n%(payload)s')

    def __init__(self, message=None, **substitute):
        payload = substitute.pop('message', {})
        payload = pprint.pformat(message)

        super(DeployProtocolError, self).__init__(
            message, payload=payload, **substitute)


class DeployTaskError(exception.IronicException):
    _msg_fmt = _(
        'Node deploy task "%(name)s" have failed: %(details)s')
