# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from twisted.internet import defer, reactor

from synapse.api.errors import AuthError, CodeMessageException, SynapseError
from synapse.util.logcontext import run_in_background
from synapse.types import UserID, get_domain_from_id

from ._base import BaseHandler

from signedjson.sign import sign_json

logger = logging.getLogger(__name__)


class ProfileHandler(BaseHandler):
    PROFILE_UPDATE_MS = 60 * 1000
    PROFILE_UPDATE_EVERY_MS = 24 * 60 * 60 * 1000

    PROFILE_REPLICATE_INTERVAL = 2 * 60 * 1000

    def __init__(self, hs):
        super(ProfileHandler, self).__init__(hs)

        self.federation = hs.get_federation_client()
        hs.get_federation_registry().register_query_handler(
            "profile", self.on_profile_query
        )

        self.user_directory_handler = hs.get_user_directory_handler()

        self.http_client = hs.get_simple_http_client()

        if hs.config.worker_app is None:
            self.clock.looping_call(
                self._update_remote_profile_cache, self.PROFILE_UPDATE_MS,
            )

            if len(self.hs.config.replicate_user_profiles_to) > 0:
                reactor.callWhenRunning(self._assign_profile_replication_batches)
                reactor.callWhenRunning(self._replicate_profiles)
                # Add a looping call to replicate_profiles: this handles retries
                # if the replication is unsuccessful when the user updated their
                # profile.
                self.clock.looping_call(
                    self._replicate_profiles, self.PROFILE_REPLICATE_INTERVAL
                )

    @defer.inlineCallbacks
    def _assign_profile_replication_batches(self):
        """If no profile replication has been done yet, allocate replication batch
        numbers to each profile to start the replication process.
        """
        logger.info("Assigning profile batch numbers...")
        total = 0
        while True:
            assigned = yield self.store.assign_profile_batch()
            total += assigned
            if assigned == 0:
                break
        logger.info("Assigned %d profile batch numbers", total)

    @defer.inlineCallbacks
    def _replicate_profiles(self):
        """If any profile data has been updated and not pushed to the replication targets,
        replicate it.
        """
        host_batches = yield self.store.get_replication_hosts()
        latest_batch = yield self.store.get_latest_profile_replication_batch_number()
        if latest_batch is None:
            latest_batch = -1
        for repl_host in self.hs.config.replicate_user_profiles_to:
            if repl_host not in host_batches:
                host_batches[repl_host] = -1
            try:
                for i in xrange(host_batches[repl_host] + 1, latest_batch + 1):
                    yield self._replicate_host_profile_batch(repl_host, i)
            except Exception:
                logger.exception(
                    "Exception while replicating to %s: aborting for now", repl_host,
                )

    @defer.inlineCallbacks
    def _replicate_host_profile_batch(self, host, batchnum):
        logger.info("Replicating profile batch %d to %s", batchnum, host)
        batch_rows = yield self.store.get_profile_batch(batchnum)
        batch = {
            UserID(r["user_id"], self.hs.hostname).to_string(): ({
                "display_name": r["displayname"],
                "avatar_url": r["avatar_url"],
            } if r["active"] else None) for r in batch_rows
        }

        url = "https://%s/_matrix/identity/api/v1/replicate_profiles" % (host,)
        body = {
            "batchnum": batchnum,
            "batch": batch,
            "origin_server": self.hs.hostname,
        }
        signed_body = sign_json(body, self.hs.hostname, self.hs.config.signing_key[0])
        try:
            yield self.http_client.post_json_get_json(url, signed_body)
            yield self.store.update_replication_batch_for_host(host, batchnum)
            logger.info("Sucessfully replicated profile batch %d to %s", batchnum, host)
        except Exception:
            # This will get retried when the looping call next comes around
            logger.exception("Failed to replicate profile batch %d to %s", batchnum, host)
            raise

    @defer.inlineCallbacks
    def get_profile(self, user_id):
        target_user = UserID.from_string(user_id)
        if self.hs.is_mine(target_user):
            displayname = yield self.store.get_profile_displayname(
                target_user.localpart
            )
            avatar_url = yield self.store.get_profile_avatar_url(
                target_user.localpart
            )

            defer.returnValue({
                "displayname": displayname,
                "avatar_url": avatar_url,
            })
        else:
            try:
                result = yield self.federation.make_query(
                    destination=target_user.domain,
                    query_type="profile",
                    args={
                        "user_id": user_id,
                    },
                    ignore_backoff=True,
                )
                defer.returnValue(result)
            except CodeMessageException as e:
                if e.code != 404:
                    logger.exception("Failed to get displayname")

                raise

    @defer.inlineCallbacks
    def get_profile_from_cache(self, user_id):
        """Get the profile information from our local cache. If the user is
        ours then the profile information will always be corect. Otherwise,
        it may be out of date/missing.
        """
        target_user = UserID.from_string(user_id)
        if self.hs.is_mine(target_user):
            displayname = yield self.store.get_profile_displayname(
                target_user.localpart
            )
            avatar_url = yield self.store.get_profile_avatar_url(
                target_user.localpart
            )

            defer.returnValue({
                "displayname": displayname,
                "avatar_url": avatar_url,
            })
        else:
            profile = yield self.store.get_from_remote_profile_cache(user_id)
            defer.returnValue(profile or {})

    @defer.inlineCallbacks
    def get_displayname(self, target_user):
        if self.hs.is_mine(target_user):
            displayname = yield self.store.get_profile_displayname(
                target_user.localpart
            )

            defer.returnValue(displayname)
        else:
            try:
                result = yield self.federation.make_query(
                    destination=target_user.domain,
                    query_type="profile",
                    args={
                        "user_id": target_user.to_string(),
                        "field": "displayname",
                    },
                    ignore_backoff=True,
                )
            except CodeMessageException as e:
                if e.code != 404:
                    logger.exception("Failed to get displayname")

                raise
            except Exception:
                logger.exception("Failed to get displayname")
            else:
                defer.returnValue(result["displayname"])

    @defer.inlineCallbacks
    def set_displayname(self, target_user, requester, new_displayname, by_admin=False):
        """target_user is the UserID whose displayname is to be changed;
        requester is the authenticated user attempting to make this change."""
        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "User is not hosted on this Home Server")

        if not by_admin and requester and target_user != requester.user:
            raise AuthError(400, "Cannot set another user's displayname")

        if not by_admin and self.hs.config.disable_set_displayname:
            profile = yield self.store.get_profileinfo(target_user.localpart)
            if profile.display_name:
                raise SynapseError(400, "Changing displayname is disabled on this server")

        if new_displayname == '':
            new_displayname = None

        if len(self.hs.config.replicate_user_profiles_to) > 0:
            cur_batchnum = yield self.store.get_latest_profile_replication_batch_number()
            new_batchnum = 0 if cur_batchnum is None else cur_batchnum + 1
        else:
            new_batchnum = None

        yield self.store.set_profile_displayname(
            target_user.localpart, new_displayname, new_batchnum
        )

        if self.hs.config.user_directory_search_all_users:
            profile = yield self.store.get_profileinfo(target_user.localpart)
            yield self.user_directory_handler.handle_local_profile_change(
                target_user.to_string(), profile
            )

        if requester:
            yield self._update_join_states(requester, target_user)

        # start a profile replication push
        run_in_background(self._replicate_profiles)

    @defer.inlineCallbacks
    def set_active(self, target_user, active):
        """
        Sets the 'active' flag on a user profile. If set to false, the user account is
        considered deactivated.
        Note that unlike set_displayname and set_avatar_url, this does *not* perform
        authorization checks! This is because the only place it's used currently is
        in account deactivation where we've already done these checks anyway.
        """
        if len(self.hs.config.replicate_user_profiles_to) > 0:
            cur_batchnum = yield self.store.get_latest_profile_replication_batch_number()
            new_batchnum = 0 if cur_batchnum is None else cur_batchnum + 1
        else:
            new_batchnum = None
        yield self.store.set_profile_active(
            target_user.localpart, active, new_batchnum
        )

        # start a profile replication push
        run_in_background(self._replicate_profiles)

    @defer.inlineCallbacks
    def get_avatar_url(self, target_user):
        if self.hs.is_mine(target_user):
            avatar_url = yield self.store.get_profile_avatar_url(
                target_user.localpart
            )

            defer.returnValue(avatar_url)
        else:
            try:
                result = yield self.federation.make_query(
                    destination=target_user.domain,
                    query_type="profile",
                    args={
                        "user_id": target_user.to_string(),
                        "field": "avatar_url",
                    },
                    ignore_backoff=True,
                )
            except CodeMessageException as e:
                if e.code != 404:
                    logger.exception("Failed to get avatar_url")
                raise
            except Exception:
                logger.exception("Failed to get avatar_url")

            defer.returnValue(result["avatar_url"])

    @defer.inlineCallbacks
    def set_avatar_url(self, target_user, requester, new_avatar_url, by_admin=False):
        """target_user is the user whose avatar_url is to be changed;
        auth_user is the user attempting to make this change."""
        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "User is not hosted on this Home Server")

        if not by_admin and target_user != requester.user:
            raise AuthError(400, "Cannot set another user's avatar_url")

        if not by_admin and self.hs.config.disable_set_avatar_url:
            profile = yield self.store.get_profileinfo(target_user.localpart)
            if profile.avatar_url:
                raise SynapseError(400, "Changing avatar url is disabled on this server")

        if len(self.hs.config.replicate_user_profiles_to) > 0:
            cur_batchnum = yield self.store.get_latest_profile_replication_batch_number()
            new_batchnum = 0 if cur_batchnum is None else cur_batchnum + 1
        else:
            new_batchnum = None

        yield self.store.set_profile_avatar_url(
            target_user.localpart, new_avatar_url, new_batchnum,
        )

        if self.hs.config.user_directory_search_all_users:
            profile = yield self.store.get_profileinfo(target_user.localpart)
            yield self.user_directory_handler.handle_local_profile_change(
                target_user.to_string(), profile
            )

        yield self._update_join_states(requester, target_user)

        # start a profile replication push
        run_in_background(self._replicate_profiles)

    @defer.inlineCallbacks
    def on_profile_query(self, args):
        user = UserID.from_string(args["user_id"])
        if not self.hs.is_mine(user):
            raise SynapseError(400, "User is not hosted on this Home Server")

        just_field = args.get("field", None)

        response = {}

        if just_field is None or just_field == "displayname":
            response["displayname"] = yield self.store.get_profile_displayname(
                user.localpart
            )

        if just_field is None or just_field == "avatar_url":
            response["avatar_url"] = yield self.store.get_profile_avatar_url(
                user.localpart
            )

        defer.returnValue(response)

    @defer.inlineCallbacks
    def _update_join_states(self, requester, target_user):
        if not self.hs.is_mine(target_user):
            return

        yield self.ratelimit(requester)

        room_ids = yield self.store.get_rooms_for_user(
            target_user.to_string(),
        )

        for room_id in room_ids:
            handler = self.hs.get_room_member_handler()
            try:
                # Assume the target_user isn't a guest,
                # because we don't let guests set profile or avatar data.
                yield handler.update_membership(
                    requester,
                    target_user,
                    room_id,
                    "join",  # We treat a profile update like a join.
                    ratelimit=False,  # Try to hide that these events aren't atomic.
                )
            except Exception as e:
                logger.warn(
                    "Failed to update join event for room %s - %s",
                    room_id, str(e.message)
                )

    def _update_remote_profile_cache(self):
        """Called periodically to check profiles of remote users we haven't
        checked in a while.
        """
        entries = yield self.store.get_remote_profile_cache_entries_that_expire(
            last_checked=self.clock.time_msec() - self.PROFILE_UPDATE_EVERY_MS
        )

        for user_id, displayname, avatar_url in entries:
            is_subscribed = yield self.store.is_subscribed_remote_profile_for_user(
                user_id,
            )
            if not is_subscribed:
                yield self.store.maybe_delete_remote_profile_cache(user_id)
                continue

            try:
                profile = yield self.federation.make_query(
                    destination=get_domain_from_id(user_id),
                    query_type="profile",
                    args={
                        "user_id": user_id,
                    },
                    ignore_backoff=True,
                )
            except Exception:
                logger.exception("Failed to get avatar_url")

                yield self.store.update_remote_profile_cache(
                    user_id, displayname, avatar_url
                )
                continue

            new_name = profile.get("displayname")
            new_avatar = profile.get("avatar_url")

            # We always hit update to update the last_check timestamp
            yield self.store.update_remote_profile_cache(
                user_id, new_name, new_avatar
            )
