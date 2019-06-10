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

import functools

import os_traits
from oslo_log import log as logging
from oslo_utils import strutils
from oslo_utils import timeutils

import nova.conf
from nova import exception
from nova.i18n import _
from nova import objects


CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)
TENANT_METADATA_KEY = 'filter_tenant_id'


def trace_request_filter(fn):
    @functools.wraps(fn)
    def wrapper(ctxt, request_spec):
        timer = timeutils.StopWatch()
        ran = False
        with timer:
            try:
                ran = fn(ctxt, request_spec)
            finally:
                if ran:
                    # Only log info if the filter was enabled and not
                    # excluded for some reason
                    LOG.debug('Request filter %r took %.1f seconds',
                        fn.__name__, timer.elapsed())
        return ran
    return wrapper


@trace_request_filter
def require_tenant_aggregate(ctxt, request_spec):
    """Require hosts in an aggregate based on tenant id.

    This will modify request_spec to request hosts in an aggregate
    defined specifically for the tenant making the request. We do that
    by looking for a nova host aggregate with metadata indicating which
    tenant it is for, and passing that aggregate uuid to placement to
    limit results accordingly.
    """

    enabled = CONF.scheduler.limit_tenants_to_placement_aggregate
    agg_required = CONF.scheduler.placement_aggregate_required_for_tenants
    if not enabled:
        return False

    aggregates = objects.AggregateList.get_by_metadata(
        ctxt, value=request_spec.project_id)
    aggregate_uuids_for_tenant = set([])
    for agg in aggregates:
        for key, value in agg.metadata.items():
            if key.startswith(TENANT_METADATA_KEY):
                aggregate_uuids_for_tenant.add(agg.uuid)
                break

    if aggregate_uuids_for_tenant:
        if ('requested_destination' not in request_spec or
                request_spec.requested_destination is None):
            request_spec.requested_destination = objects.Destination()
        destination = request_spec.requested_destination
        destination.require_aggregates(aggregate_uuids_for_tenant)
        LOG.debug('require_tenant_aggregate request filter added '
                  'aggregates %s for tenant %r',
                  ','.join(aggregate_uuids_for_tenant),
                  request_spec.project_id)
    elif agg_required:
        LOG.warning('Tenant %(tenant)s has no available aggregates',
                    {'tenant': request_spec.project_id})
        raise exception.RequestFilterFailed(
            reason=_('No hosts available for tenant'))

    return True


@trace_request_filter
def map_az_to_placement_aggregate(ctxt, request_spec):
    """Map requested nova availability zones to placement aggregates.

    This will modify request_spec to request hosts in an aggregate that
    matches the desired AZ of the user's request.
    """
    if not CONF.scheduler.query_placement_for_availability_zone:
        return False

    az_hint = request_spec.availability_zone
    if not az_hint:
        return False

    aggregates = objects.AggregateList.get_by_metadata(ctxt,
                                                       key='availability_zone',
                                                       value=az_hint)
    if aggregates:
        if ('requested_destination' not in request_spec or
                request_spec.requested_destination is None):
            request_spec.requested_destination = objects.Destination()
        agg_uuids = [agg.uuid for agg in aggregates]
        request_spec.requested_destination.require_aggregates(agg_uuids)
        LOG.debug('map_az_to_placement_aggregate request filter added '
                  'aggregates %s for az %r',
                  ','.join(agg_uuids),
                  az_hint)

    return True


@trace_request_filter
def require_image_type_support(ctxt, request_spec):
    """Request type-specific trait on candidates.

    This will modify the request_spec to request hosts that support the
    disk_format of the image provided.
    """
    if not CONF.scheduler.query_placement_for_image_type_support:
        return False

    if request_spec.is_bfv:
        # We are booting from volume, and thus compute node image
        # disk_format support does not matter.
        return False

    disk_format = request_spec.image.disk_format
    trait_name = 'COMPUTE_IMAGE_TYPE_%s' % disk_format.upper()
    if not hasattr(os_traits, trait_name):
        LOG.error(('Computed trait name %r is not valid; '
                   'is os-traits up to date?'), trait_name)
        return False

    # NOTE(danms): We are using the transient flavor in the request spec
    # to add the trait that we need. We make sure that we reset the dirty-ness
    # of this field to avoid persisting it.
    request_spec.flavor.extra_specs['trait:%s' % trait_name] = 'required'
    request_spec.obj_reset_changes(fields=['flavor'], recursive=True)

    LOG.debug('require_image_type_support request filter added required '
              'trait %s', trait_name)

    return True


def _mem_encryption_extra_spec(request_spec):
    # Return None if hw:mem_encryption extra spec is not
    # explicitly set, otherwise the boolean it's set to.
    if 'flavor' not in request_spec:
        return None

    if 'extra_specs' not in request_spec.flavor:
        return None

    val = request_spec.flavor.extra_specs.get('hw:mem_encryption')
    return val if val is None else strutils.bool_from_string(val)


def _mem_encryption_image_prop(request_spec):
    # Return None if hw_mem_encryption image property is not
    # explicitly set, otherwise the boolean it's set to.
    if 'image' not in request_spec:
        return None

    if 'properties' not in request_spec.image:
        return None

    # FlexibleBooleanField so coercion to a boolean is handled automatically
    return request_spec.image.properties.get('hw_mem_encryption')


@trace_request_filter
def require_encrypted_memory_support(ctxt, request_spec):
    """When the hw:mem_encryption extra spec or the hw_mem_encryption
    image property are requested, require hosts which can support
    encryption of the guest memory.
    """
    extra_spec = _mem_encryption_extra_spec(request_spec)
    image_prop = _mem_encryption_image_prop(request_spec)

    if not extra_spec and not image_prop:
        # Neither require memory encryption, so no further action required.
        return False

    # Check for conflicts between explicit requirements regarding
    # memory encryption.
    if (extra_spec is not None and image_prop is not None and
            extra_spec != image_prop):
        # Unfortunately there's no way to avoid this duplication :-(
        lmsg = (
            "Flavor %(flavor_name)s has hw:mem_encryption extra spec "
            "explicitly set to %(flavor_val)s, conflicting with "
            "image %(image_name)s which has hw_mem_encryption property "
            "explicitly set to %(image_val)s"
        )
        emsg = _(
            "Flavor %(flavor_name)s has hw:mem_encryption extra spec "
            "explicitly set to %(flavor_val)s, conflicting with "
            "image %(image_name)s which has hw_mem_encryption property "
            "explicitly set to %(image_val)s"
        )
        data = {
            'flavor_name': request_spec.flavor.name,
            'flavor_val':
                request_spec.flavor.extra_specs.get('hw:mem_encryption'),
            'image_name': request_spec.image.name,
            'image_val':
                request_spec.image.properties.get('hw_mem_encryption')
        }
        LOG.error(lmsg, data)
        raise exception.RequestFilterFailed(reason=emsg % data)

    # If we get this far, either the extra spec or image property explicitly
    # specified a requirement regarding memory encryption, and if both did,
    # they are asking for the same thing.
    request_spec.flavor.extra_specs['resources:MEM_ENCRYPTION_CONTEXT'] = '1'

    requesters = []
    if extra_spec:
        requesters.append("hw:mem_encryption extra spec")
    if image_prop:
        requesters.append("hw_mem_encryption image property")

    if not requesters:
        # This should never happen if the logic above is correct
        LOG.error("BUG: require_encrypted_memory_support "
                  "request filter was applied for unknown reason")
    else:
        LOG.debug("require_encrypted_memory_support request filter added "
                  "required resource MEM_ENCRYPTION_CONTEXT due to %s",
                  " and ".join(requesters))

    return True


ALL_REQUEST_FILTERS = [
    require_tenant_aggregate,
    map_az_to_placement_aggregate,
    require_image_type_support,
    require_encrypted_memory_support,
]


def process_reqspec(ctxt, request_spec):
    """Process an objects.ReqestSpec before calling placement.

    :param ctxt: A RequestContext
    :param request_spec: An objects.RequestSpec to be inspected/modified
    """
    for filter in ALL_REQUEST_FILTERS:
        filter(ctxt, request_spec)
