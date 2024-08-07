from typing import Any, Dict, Mapping, Optional, Union

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe
from pydantic import Json, NonNegativeInt, StringConstraints
from pydantic.functional_validators import AfterValidator
from typing_extensions import Annotated

from confirmation.models import Confirmation, ConfirmationKeyError, get_object_from_key
from zerver.actions.create_realm import do_change_realm_subdomain
from zerver.actions.realm_settings import (
    do_change_realm_org_type,
    do_change_realm_permission_group_setting,
    do_deactivate_realm,
    do_reactivate_realm,
    do_set_realm_authentication_methods,
    do_set_realm_new_stream_announcements_stream,
    do_set_realm_property,
    do_set_realm_signup_announcements_stream,
    do_set_realm_user_default_setting,
    do_set_realm_zulip_update_announcements_stream,
    parse_and_set_setting_value_if_required,
    validate_authentication_methods_dict_from_api,
    validate_plan_for_authentication_methods,
)
from zerver.decorator import require_realm_admin, require_realm_owner
from zerver.forms import check_subdomain_available as check_subdomain
from zerver.lib.exceptions import JsonableError, OrganizationOwnerRequiredError
from zerver.lib.i18n import get_available_language_codes
from zerver.lib.request import REQ, has_request_variables
from zerver.lib.response import json_success
from zerver.lib.retention import parse_message_retention_days
from zerver.lib.streams import access_stream_by_id
from zerver.lib.typed_endpoint import ApiParamConfig, typed_endpoint
from zerver.lib.user_groups import (
    GroupSettingChangeRequest,
    access_user_group_for_setting,
    get_group_setting_value_for_api,
    parse_group_setting_value,
    validate_group_setting_value_change,
)
from zerver.lib.validator import (
    check_bool,
    check_capped_url,
    check_int,
    check_int_in,
    check_string,
    check_string_in,
)
from zerver.models import Realm, RealmReactivationStatus, RealmUserDefault, UserProfile
from zerver.models.realms import (
    BotCreationPolicyEnum,
    CommonMessagePolicyEnum,
    CommonPolicyEnum,
    CreateWebPublicStreamPolicyEnum,
    DigestWeekdayEnum,
    EditTopicPolicyEnum,
    InviteToRealmPolicyEnum,
    MoveMessagesBetweenStreamsPolicyEnum,
    OrgTypeEnum,
    WildcardMentionPolicyEnum,
)
from zerver.views.user_settings import check_settings_values


def parse_jitsi_server_url(
    value: str, special_values_map: Mapping[str, Optional[str]]
) -> Optional[str]:
    if value in special_values_map:
        return special_values_map[value]

    return value


JITSI_SERVER_URL_MAX_LENGTH = 200


def check_jitsi_url(value: str) -> str:
    var_name = "jitsi_server_url"
    value = check_string(var_name, value)

    if value in list(Realm.JITSI_SERVER_SPECIAL_VALUES_MAP.keys()):
        return value

    validator = check_capped_url(JITSI_SERVER_URL_MAX_LENGTH)
    try:
        return validator(var_name, value)
    except ValidationError:
        raise JsonableError(_("{var_name} is not an allowed_type").format(var_name=var_name))


@require_realm_admin
@typed_endpoint
def update_realm(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    name: Annotated[
        Optional[str], StringConstraints(max_length=Realm.MAX_REALM_NAME_LENGTH)
    ] = None,
    description: Annotated[
        Optional[str], StringConstraints(max_length=Realm.MAX_REALM_DESCRIPTION_LENGTH)
    ] = None,
    emails_restricted_to_domains: Optional[Json[bool]] = None,
    disallow_disposable_email_addresses: Optional[Json[bool]] = None,
    invite_required: Optional[Json[bool]] = None,
    invite_to_realm_policy: Optional[Json[InviteToRealmPolicyEnum]] = None,
    create_multiuse_invite_group_id: Annotated[
        Optional[Json[int]], ApiParamConfig(whence="create_multiuse_invite_group")
    ] = None,
    require_unique_names: Optional[Json[bool]] = None,
    name_changes_disabled: Optional[Json[bool]] = None,
    email_changes_disabled: Optional[Json[bool]] = None,
    avatar_changes_disabled: Optional[Json[bool]] = None,
    inline_image_preview: Optional[Json[bool]] = None,
    inline_url_embed_preview: Optional[Json[bool]] = None,
    add_custom_emoji_policy: Optional[Json[CommonPolicyEnum]] = None,
    delete_own_message_policy: Optional[Json[CommonMessagePolicyEnum]] = None,
    message_content_delete_limit_seconds_raw: Annotated[
        Optional[Json[Union[int, str]]],
        ApiParamConfig("message_content_delete_limit_seconds"),
    ] = None,
    allow_message_editing: Optional[Json[bool]] = None,
    edit_topic_policy: Optional[Json[EditTopicPolicyEnum]] = None,
    mandatory_topics: Optional[Json[bool]] = None,
    message_content_edit_limit_seconds_raw: Annotated[
        Optional[Json[Union[int, str]]], ApiParamConfig("message_content_edit_limit_seconds")
    ] = None,
    allow_edit_history: Optional[Json[bool]] = None,
    default_language: Optional[str] = None,
    waiting_period_threshold: Optional[Json[NonNegativeInt]] = None,
    authentication_methods: Optional[Json[Dict[str, Any]]] = None,
    # Note: push_notifications_enabled and push_notifications_enabled_end_timestamp
    # are not offered here as it is maintained by the server, not via the API.
    new_stream_announcements_stream_id: Optional[Json[int]] = None,
    signup_announcements_stream_id: Optional[Json[int]] = None,
    zulip_update_announcements_stream_id: Optional[Json[int]] = None,
    message_retention_days_raw: Annotated[
        Optional[Json[Union[int, str]]], ApiParamConfig("message_retention_days")
    ] = None,
    send_welcome_emails: Optional[Json[bool]] = None,
    digest_emails_enabled: Optional[Json[bool]] = None,
    message_content_allowed_in_email_notifications: Optional[Json[bool]] = None,
    bot_creation_policy: Optional[Json[BotCreationPolicyEnum]] = None,
    can_create_public_channel_group: Optional[Json[GroupSettingChangeRequest]] = None,
    can_create_private_channel_group: Optional[Json[GroupSettingChangeRequest]] = None,
    direct_message_initiator_group: Optional[Json[GroupSettingChangeRequest]] = None,
    direct_message_permission_group: Optional[Json[GroupSettingChangeRequest]] = None,
    create_web_public_stream_policy: Optional[Json[CreateWebPublicStreamPolicyEnum]] = None,
    invite_to_stream_policy: Optional[Json[CommonPolicyEnum]] = None,
    move_messages_between_streams_policy: Optional[
        Json[MoveMessagesBetweenStreamsPolicyEnum]
    ] = None,
    user_group_edit_policy: Optional[Json[CommonPolicyEnum]] = None,
    wildcard_mention_policy: Optional[Json[WildcardMentionPolicyEnum]] = None,
    video_chat_provider: Optional[Json[int]] = None,
    jitsi_server_url_raw: Annotated[
        Optional[Json[str]],
        AfterValidator(lambda val: check_jitsi_url(val)),
        ApiParamConfig("jitsi_server_url"),
    ] = None,
    giphy_rating: Optional[Json[int]] = None,
    default_code_block_language: Optional[str] = None,
    digest_weekday: Optional[Json[DigestWeekdayEnum]] = None,
    string_id: Annotated[
        Optional[str], StringConstraints(max_length=Realm.MAX_REALM_SUBDOMAIN_LENGTH)
    ] = None,
    org_type: Optional[Json[OrgTypeEnum]] = None,
    enable_spectator_access: Optional[Json[bool]] = None,
    want_advertise_in_communities_directory: Optional[Json[bool]] = None,
    enable_read_receipts: Optional[Json[bool]] = None,
    move_messages_within_stream_limit_seconds_raw: Annotated[
        Optional[Json[Union[int, str]]],
        ApiParamConfig("move_messages_within_stream_limit_seconds"),
    ] = None,
    move_messages_between_streams_limit_seconds_raw: Annotated[
        Optional[Json[Union[int, str]]],
        ApiParamConfig("move_messages_between_streams_limit_seconds"),
    ] = None,
    enable_guest_user_indicator: Optional[Json[bool]] = None,
    can_access_all_users_group_id: Annotated[
        Optional[Json[int]], ApiParamConfig("can_access_all_users_group")
    ] = None,
) -> HttpResponse:
    realm = user_profile.realm

    # Additional validation/error checking beyond types go here, so
    # the entire request can succeed or fail atomically.
    if default_language is not None and default_language not in get_available_language_codes():
        raise JsonableError(_("Invalid language '{language}'").format(language=default_language))
    if authentication_methods is not None:
        if not user_profile.is_realm_owner:
            raise OrganizationOwnerRequiredError

        validate_authentication_methods_dict_from_api(realm, authentication_methods)
        if True not in authentication_methods.values():
            raise JsonableError(_("At least one authentication method must be enabled."))
        validate_plan_for_authentication_methods(realm, authentication_methods)

    if video_chat_provider is not None and video_chat_provider not in {
        p["id"] for p in Realm.VIDEO_CHAT_PROVIDERS.values()
    }:
        raise JsonableError(
            _("Invalid video_chat_provider {video_chat_provider}").format(
                video_chat_provider=video_chat_provider
            )
        )
    if giphy_rating is not None and giphy_rating not in {
        p["id"] for p in Realm.GIPHY_RATING_OPTIONS.values()
    }:
        raise JsonableError(
            _("Invalid giphy_rating {giphy_rating}").format(giphy_rating=giphy_rating)
        )

    message_retention_days: Optional[int] = None
    if message_retention_days_raw is not None:
        if not user_profile.is_realm_owner:
            raise OrganizationOwnerRequiredError
        realm.ensure_not_on_limited_plan()
        message_retention_days = parse_message_retention_days(  # used by locals() below
            message_retention_days_raw, Realm.MESSAGE_RETENTION_SPECIAL_VALUES_MAP
        )

    if (
        invite_to_realm_policy is not None
        or invite_required is not None
        or create_multiuse_invite_group_id is not None
    ) and not user_profile.is_realm_owner:
        raise OrganizationOwnerRequiredError

    if (
        emails_restricted_to_domains is not None or disallow_disposable_email_addresses is not None
    ) and not user_profile.is_realm_owner:
        raise OrganizationOwnerRequiredError

    if waiting_period_threshold is not None and not user_profile.is_realm_owner:
        raise OrganizationOwnerRequiredError

    if enable_spectator_access:
        realm.ensure_not_on_limited_plan()

    if can_access_all_users_group_id is not None:
        realm.can_enable_restricted_user_access_for_guests()

    data: Dict[str, Any] = {}

    message_content_delete_limit_seconds: Optional[int] = None
    if message_content_delete_limit_seconds_raw is not None:
        (
            message_content_delete_limit_seconds,
            setting_value_changed,
        ) = parse_and_set_setting_value_if_required(
            realm,
            "message_content_delete_limit_seconds",
            message_content_delete_limit_seconds_raw,
            acting_user=user_profile,
        )

        if setting_value_changed:
            data["message_content_delete_limit_seconds"] = message_content_delete_limit_seconds

    message_content_edit_limit_seconds: Optional[int] = None
    if message_content_edit_limit_seconds_raw is not None:
        (
            message_content_edit_limit_seconds,
            setting_value_changed,
        ) = parse_and_set_setting_value_if_required(
            realm,
            "message_content_edit_limit_seconds",
            message_content_edit_limit_seconds_raw,
            acting_user=user_profile,
        )

        if setting_value_changed:
            data["message_content_edit_limit_seconds"] = message_content_edit_limit_seconds

    move_messages_within_stream_limit_seconds: Optional[int] = None
    if move_messages_within_stream_limit_seconds_raw is not None:
        (
            move_messages_within_stream_limit_seconds,
            setting_value_changed,
        ) = parse_and_set_setting_value_if_required(
            realm,
            "move_messages_within_stream_limit_seconds",
            move_messages_within_stream_limit_seconds_raw,
            acting_user=user_profile,
        )

        if setting_value_changed:
            data["move_messages_within_stream_limit_seconds"] = (
                move_messages_within_stream_limit_seconds
            )

    move_messages_between_streams_limit_seconds: Optional[int] = None
    if move_messages_between_streams_limit_seconds_raw is not None:
        (
            move_messages_between_streams_limit_seconds,
            setting_value_changed,
        ) = parse_and_set_setting_value_if_required(
            realm,
            "move_messages_between_streams_limit_seconds",
            move_messages_between_streams_limit_seconds_raw,
            acting_user=user_profile,
        )

        if setting_value_changed:
            data["move_messages_between_streams_limit_seconds"] = (
                move_messages_between_streams_limit_seconds
            )

    jitsi_server_url: Optional[str] = None
    if jitsi_server_url_raw is not None:
        jitsi_server_url = parse_jitsi_server_url(
            jitsi_server_url_raw,
            Realm.JITSI_SERVER_SPECIAL_VALUES_MAP,
        )

        # We handle the "None" case separately here because
        # in the loop below, do_set_realm_property is called only when
        # the setting value is not "None". For values other than "None",
        # the loop itself sets the value of 'jitsi_server_url' by
        # calling do_set_realm_property.
        if jitsi_server_url is None and realm.jitsi_server_url is not None:
            do_set_realm_property(
                realm,
                "jitsi_server_url",
                jitsi_server_url,
                acting_user=user_profile,
            )

            data["jitsi_server_url"] = jitsi_server_url

    # The user of `locals()` here is a bit of a code smell, but it's
    # restricted to the elements present in realm.property_types.
    #
    # TODO: It should be possible to deduplicate this function up
    # further by some more advanced usage of the
    # `REQ/has_request_variables` extraction.
    req_vars = {}
    req_group_setting_vars = {}

    for k, v in locals().items():
        if k in realm.property_types:
            req_vars[k] = v

        for setting_name, permission_configuration in Realm.REALM_PERMISSION_GROUP_SETTINGS.items():
            if k in [permission_configuration.id_field_name, setting_name]:
                req_group_setting_vars[k] = v

    for k, v in req_vars.items():
        if v is not None and getattr(realm, k) != v:
            do_set_realm_property(realm, k, v, acting_user=user_profile)
            if isinstance(v, str):
                data[k] = "updated"
            else:
                data[k] = v

    for setting_name, permission_configuration in Realm.REALM_PERMISSION_GROUP_SETTINGS.items():
        setting_group_id_name = permission_configuration.id_field_name

        expected_current_setting_value = None
        if setting_name in Realm.REALM_PERMISSION_GROUP_SETTINGS_WITH_NEW_API_FORMAT:
            assert setting_name in req_group_setting_vars
            if req_group_setting_vars[setting_name] is None:
                continue

            setting_value = req_group_setting_vars[setting_name]
            new_setting_value = parse_group_setting_value(setting_value.new, setting_name)

            if setting_value.old is not None:
                expected_current_setting_value = parse_group_setting_value(
                    setting_value.old, setting_name
                )
        else:
            assert setting_group_id_name in req_group_setting_vars
            if req_group_setting_vars[setting_group_id_name] is None:
                continue
            new_setting_value = req_group_setting_vars[setting_group_id_name]

        current_value = getattr(realm, setting_name)
        current_setting_api_value = get_group_setting_value_for_api(current_value)

        if validate_group_setting_value_change(
            current_setting_api_value, new_setting_value, expected_current_setting_value
        ):
            with transaction.atomic(durable=True):
                user_group = access_user_group_for_setting(
                    new_setting_value,
                    user_profile,
                    setting_name=setting_name,
                    permission_configuration=permission_configuration,
                    current_setting_value=current_value,
                )
                do_change_realm_permission_group_setting(
                    realm,
                    setting_name,
                    user_group,
                    old_setting_api_value=current_setting_api_value,
                    acting_user=user_profile,
                )
            data[setting_name] = new_setting_value

    # The following realm properties do not fit the pattern above
    # authentication_methods is not supported by the do_set_realm_property
    # framework because it's tracked through the RealmAuthenticationMethod table.
    if authentication_methods is not None and (
        realm.authentication_methods_dict() != authentication_methods
    ):
        do_set_realm_authentication_methods(realm, authentication_methods, acting_user=user_profile)
        data["authentication_methods"] = authentication_methods

    # Realm.new_stream_announcements_stream, Realm.signup_announcements_stream,
    # and Realm.zulip_update_announcements_stream are not boolean, str or integer field,
    # and thus doesn't fit into the do_set_realm_property framework.
    if new_stream_announcements_stream_id is not None and (
        realm.new_stream_announcements_stream is None
        or (realm.new_stream_announcements_stream.id != new_stream_announcements_stream_id)
    ):
        new_stream_announcements_stream_new = None
        if new_stream_announcements_stream_id >= 0:
            (new_stream_announcements_stream_new, sub) = access_stream_by_id(
                user_profile, new_stream_announcements_stream_id, allow_realm_admin=True
            )
        do_set_realm_new_stream_announcements_stream(
            realm,
            new_stream_announcements_stream_new,
            new_stream_announcements_stream_id,
            acting_user=user_profile,
        )
        data["new_stream_announcements_stream_id"] = new_stream_announcements_stream_id

    if signup_announcements_stream_id is not None and (
        realm.signup_announcements_stream is None
        or realm.signup_announcements_stream.id != signup_announcements_stream_id
    ):
        new_signup_announcements_stream = None
        if signup_announcements_stream_id >= 0:
            (new_signup_announcements_stream, sub) = access_stream_by_id(
                user_profile, signup_announcements_stream_id, allow_realm_admin=True
            )
        do_set_realm_signup_announcements_stream(
            realm,
            new_signup_announcements_stream,
            signup_announcements_stream_id,
            acting_user=user_profile,
        )
        data["signup_announcements_stream_id"] = signup_announcements_stream_id

    if zulip_update_announcements_stream_id is not None and (
        realm.zulip_update_announcements_stream is None
        or realm.zulip_update_announcements_stream.id != zulip_update_announcements_stream_id
    ):
        new_zulip_update_announcements_stream = None
        if zulip_update_announcements_stream_id >= 0:
            (new_zulip_update_announcements_stream, sub) = access_stream_by_id(
                user_profile, zulip_update_announcements_stream_id, allow_realm_admin=True
            )
        do_set_realm_zulip_update_announcements_stream(
            realm,
            new_zulip_update_announcements_stream,
            zulip_update_announcements_stream_id,
            acting_user=user_profile,
        )
        data["zulip_update_announcements_stream_id"] = zulip_update_announcements_stream_id

    if string_id is not None:
        if not user_profile.is_realm_owner:
            raise OrganizationOwnerRequiredError

        if realm.demo_organization_scheduled_deletion_date is None:
            raise JsonableError(_("Must be a demo organization."))

        try:
            check_subdomain(string_id)
        except ValidationError as err:
            raise JsonableError(str(err.message))

        do_change_realm_subdomain(realm, string_id, acting_user=user_profile)
        data["realm_uri"] = realm.url
        data["realm_url"] = realm.url

    if org_type is not None:
        do_change_realm_org_type(realm, org_type, acting_user=user_profile)
        data["org_type"] = org_type

    return json_success(request, data)


@require_realm_owner
@has_request_variables
def deactivate_realm(request: HttpRequest, user: UserProfile) -> HttpResponse:
    realm = user.realm
    do_deactivate_realm(
        realm, acting_user=user, deactivation_reason="owner_request", email_owners=True
    )
    return json_success(request)


@require_safe
def check_subdomain_available(request: HttpRequest, subdomain: str) -> HttpResponse:
    try:
        check_subdomain(subdomain)
        return json_success(request, data={"msg": "available"})
    except ValidationError as e:
        return json_success(request, data={"msg": e.message})


def realm_reactivation(request: HttpRequest, confirmation_key: str) -> HttpResponse:
    try:
        obj = get_object_from_key(
            confirmation_key, [Confirmation.REALM_REACTIVATION], mark_object_used=True
        )
    except ConfirmationKeyError:
        return render(request, "zerver/realm_reactivation_link_error.html", status=404)

    assert isinstance(obj, RealmReactivationStatus)
    realm = obj.realm

    do_reactivate_realm(realm)

    context = {"realm": realm}
    return render(request, "zerver/realm_reactivation.html", context)


emojiset_choices = {emojiset["key"] for emojiset in RealmUserDefault.emojiset_choices()}
web_home_view_options = ["recent_topics", "inbox", "all_messages"]


@require_realm_admin
@has_request_variables
def update_realm_user_settings_defaults(
    request: HttpRequest,
    user_profile: UserProfile,
    dense_mode: Optional[bool] = REQ(json_validator=check_bool, default=None),
    web_mark_read_on_scroll_policy: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.WEB_MARK_READ_ON_SCROLL_POLICY_CHOICES),
        default=None,
    ),
    web_channel_default_view: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.WEB_CHANNEL_DEFAULT_VIEW_CHOICES),
        default=None,
    ),
    starred_message_counts: Optional[bool] = REQ(json_validator=check_bool, default=None),
    receives_typing_notifications: Optional[bool] = REQ(json_validator=check_bool, default=None),
    web_stream_unreads_count_display_policy: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.WEB_STREAM_UNREADS_COUNT_DISPLAY_POLICY_CHOICES),
        default=None,
    ),
    fluid_layout_width: Optional[bool] = REQ(json_validator=check_bool, default=None),
    high_contrast_mode: Optional[bool] = REQ(json_validator=check_bool, default=None),
    color_scheme: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.COLOR_SCHEME_CHOICES), default=None
    ),
    web_font_size_px: Optional[int] = REQ(json_validator=check_int, default=None),
    web_line_height_percent: Optional[int] = REQ(json_validator=check_int, default=None),
    translate_emoticons: Optional[bool] = REQ(json_validator=check_bool, default=None),
    display_emoji_reaction_users: Optional[bool] = REQ(json_validator=check_bool, default=None),
    web_home_view: Optional[str] = REQ(
        str_validator=check_string_in(web_home_view_options), default=None
    ),
    web_escape_navigates_to_home_view: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    left_side_userlist: Optional[bool] = REQ(json_validator=check_bool, default=None),
    emojiset: Optional[str] = REQ(str_validator=check_string_in(emojiset_choices), default=None),
    demote_inactive_streams: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.DEMOTE_STREAMS_CHOICES), default=None
    ),
    enable_stream_desktop_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    enable_stream_email_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    enable_stream_push_notifications: Optional[bool] = REQ(json_validator=check_bool, default=None),
    enable_stream_audible_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    wildcard_mentions_notify: Optional[bool] = REQ(json_validator=check_bool, default=None),
    enable_followed_topic_desktop_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    enable_followed_topic_email_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    enable_followed_topic_push_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    enable_followed_topic_audible_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    enable_followed_topic_wildcard_mentions_notify: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    notification_sound: Optional[str] = REQ(default=None),
    enable_desktop_notifications: Optional[bool] = REQ(json_validator=check_bool, default=None),
    enable_sounds: Optional[bool] = REQ(json_validator=check_bool, default=None),
    enable_offline_email_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    enable_offline_push_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    enable_online_push_notifications: Optional[bool] = REQ(json_validator=check_bool, default=None),
    enable_digest_emails: Optional[bool] = REQ(json_validator=check_bool, default=None),
    # enable_login_emails is not included here, because we don't want
    # security-related settings to be controlled by organization administrators.
    # enable_marketing_emails is not included here, since we don't at
    # present allow organizations to customize this. (The user's selection
    # in the signup form takes precedence over RealmUserDefault).
    #
    # We may want to change this model in the future, since some SSO signups
    # do not offer an opportunity to prompt the user at all during signup.
    message_content_in_email_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    pm_content_in_desktop_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    desktop_icon_count_display: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.DESKTOP_ICON_COUNT_DISPLAY_CHOICES), default=None
    ),
    realm_name_in_email_notifications_policy: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.REALM_NAME_IN_EMAIL_NOTIFICATIONS_POLICY_CHOICES),
        default=None,
    ),
    automatically_follow_topics_policy: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.AUTOMATICALLY_CHANGE_VISIBILITY_POLICY_CHOICES),
        default=None,
    ),
    automatically_unmute_topics_in_muted_streams_policy: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.AUTOMATICALLY_CHANGE_VISIBILITY_POLICY_CHOICES),
        default=None,
    ),
    automatically_follow_topics_where_mentioned: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    presence_enabled: Optional[bool] = REQ(json_validator=check_bool, default=None),
    enter_sends: Optional[bool] = REQ(json_validator=check_bool, default=None),
    enable_drafts_synchronization: Optional[bool] = REQ(json_validator=check_bool, default=None),
    email_notifications_batching_period_seconds: Optional[int] = REQ(
        json_validator=check_int, default=None
    ),
    twenty_four_hour_time: Optional[bool] = REQ(json_validator=check_bool, default=None),
    send_stream_typing_notifications: Optional[bool] = REQ(json_validator=check_bool, default=None),
    send_private_typing_notifications: Optional[bool] = REQ(
        json_validator=check_bool, default=None
    ),
    send_read_receipts: Optional[bool] = REQ(json_validator=check_bool, default=None),
    user_list_style: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.USER_LIST_STYLE_CHOICES), default=None
    ),
    email_address_visibility: Optional[int] = REQ(
        json_validator=check_int_in(UserProfile.EMAIL_ADDRESS_VISIBILITY_TYPES), default=None
    ),
    web_navigate_to_sent_message: Optional[bool] = REQ(json_validator=check_bool, default=None),
) -> HttpResponse:
    if notification_sound is not None or email_notifications_batching_period_seconds is not None:
        check_settings_values(notification_sound, email_notifications_batching_period_seconds)

    realm_user_default = RealmUserDefault.objects.get(realm=user_profile.realm)
    request_settings = {k: v for k, v in locals().items() if k in RealmUserDefault.property_types}
    for k, v in request_settings.items():
        if v is not None and getattr(realm_user_default, k) != v:
            do_set_realm_user_default_setting(realm_user_default, k, v, acting_user=user_profile)

    return json_success(request)
