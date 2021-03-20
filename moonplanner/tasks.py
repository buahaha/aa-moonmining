import logging

import yaml
from app_utils.datetime import ldap_time_2_datetime
from app_utils.logging import LoggerAddTag
from celery import shared_task

from django.contrib.auth.models import User
from django.db import transaction

from allianceauth.notifications import notify
from eveuniverse.models import EveMarketPrice, EveMoon, EveSolarSystem, EveType

from . import __title__, constants
from .models import (
    Extraction,
    ExtractionProduct,
    MiningCorporation,
    Moon,
    MoonProduct,
    Refinery,
)
from .providers import esi

logger = LoggerAddTag(logging.getLogger(__name__), __title__)

MAX_DISTANCE_TO_MOON_METERS = 3000000


"""
def _get_tokens(scopes):
    try:
        tokens = []
        characters = MoonDataCharacter.objects.all()
        for character in characters:
            tokens.append(Token.objects.filter(character_id=character.character.character_id).require_scopes(scopes)[0])
        return tokens
    except Exception as e:
        print(e)
        return False
"""


@shared_task
def process_survey_input(scans, user_pk=None):
    """process raw moon survey input from user

    Args:
        scans: raw text input from user containing moon survey data
        user_pk: (optional) id of user who submitted the data
    """
    process_results = list()
    try:
        lines = scans.split("\n")
        lines_ = []
        for line in lines:
            line = line.strip("\r").split("\t")
            lines_.append(line)
        lines = lines_

        # Find all groups of scans.
        if len(lines[0]) == 0 or lines[0][0] == "Moon":
            lines = lines[1:]
        sublists = []
        for line in lines:
            # Find the lines that start a scan
            if line[0] == "":
                pass
            else:
                sublists.append(lines.index(line))

        # Separate out individual surveys
        surveys = []
        for i in range(len(sublists)):
            # The First List
            if i == 0:
                if i + 2 > len(sublists):
                    surveys.append(lines[sublists[i] :])
                else:
                    surveys.append(lines[sublists[i] : sublists[i + 1]])
            else:
                if i + 2 > len(sublists):
                    surveys.append(lines[sublists[i] :])
                else:
                    surveys.append(lines[sublists[i] : sublists[i + 1]])

    except Exception as ex:
        logger.warning(
            "An issue occurred while trying to parse the surveys", exc_info=True
        )
        error_name = type(ex).__name__
        success = False

    else:
        success = True
        error_name = None
        moon_name = None
        for survey in surveys:
            try:
                with transaction.atomic():  # TODO: remove transaction
                    moon_name = survey[0][0]
                    moon_id = survey[1][6]
                    eve_moon, _ = EveMoon.objects.get_or_create_esi(id=moon_id)
                    moon, _ = Moon.objects.get_or_create(eve_moon=eve_moon)
                    moon.products.all().delete()
                    survey = survey[1:]
                    for product_data in survey:
                        # Trim off the empty index at the front
                        product_data = product_data[1:]
                        eve_type, _ = EveType.objects.get_or_create_esi(
                            id=product_data[2],
                            enabled_sections=[EveType.Section.TYPE_MATERIALS],
                        )
                        MoonProduct.objects.create(
                            moon=moon, amount=product_data[1], eve_type=eve_type
                        )
                    moon.update_income_estimate()
                    logger.info("Added moon survey for %s", moon.eve_moon.name)

            except Exception as ex:
                logger.warning(
                    "An issue occurred while processing the following moon survey: "
                    f"{survey}",
                    exc_info=True,
                )
                error_name = type(ex).__name__
                success = False
            else:
                success = True
                error_name = None

            process_results.append(
                {"moon_name": moon_name, "success": success, "error_name": error_name}
            )

    # send result notification to user
    if user_pk:
        message = "We have completed processing your moon survey input:\n\n"
        if process_results:
            n = 0
            for result in process_results:
                n = n + 1
                name = result["moon_name"]
                if result["success"]:
                    status = "OK"
                    error_name = ""
                else:
                    status = "FAILED"
                    success = False
                    error_name = "- {}".format(result["error_name"])
                message += "#{}: {}: {} {}\n".format(n, name, status, error_name)
        else:
            message += f"\nProcessing failed: {error_name}"

        notify(
            user=User.objects.get(pk=user_pk),
            title="Moon survey input processing results: {}".format(
                "OK" if success else "FAILED"
            ),
            message=message,
            level="success" if success else "danger",
        )

    return success


@shared_task
def run_refineries_update(mining_corp_pk):
    """update list of refineries with extractions for a mining corporation"""
    mining_corp = MiningCorporation.objects.get(pk=mining_corp_pk)
    if mining_corp.character is None:
        logger.error("%s: Mining corporation has no character. Aborting", mining_corp)
        return

    token = mining_corp.fetch_token()
    logger.info("%s: Fetching corp structures from ESI", mining_corp)
    all_structures = esi.client.Corporation.get_corporations_corporation_id_structures(
        corporation_id=mining_corp.corporation.corporation_id,
        token=token.valid_access_token(),
    ).result()

    logger.info("%s: Updating refineries", mining_corp)
    user_report = list()
    for refinery in all_structures:
        eve_type, _ = EveType.objects.get_or_create_esi(id=refinery["type_id"])
        if eve_type.eve_group_id == constants.EVE_GROUP_ID_REFINERY:
            # determine moon next to refinery
            structure_info = esi.client.Universe.get_universe_structures_structure_id(
                structure_id=refinery["structure_id"],
                token=token.valid_access_token(),
            ).result()
            solar_system, _ = EveSolarSystem.objects.get_or_create_esi(
                id=structure_info["solar_system_id"]
            )
            nearest_celestial = solar_system.nearest_celestial(
                structure_info["position"]["x"],
                structure_info["position"]["y"],
                structure_info["position"]["z"],
                group_id=constants.EVE_GROUP_ID_MOON,
                max_distance=MAX_DISTANCE_TO_MOON_METERS,
            )
            if (
                nearest_celestial
                and nearest_celestial.eve_type.id == constants.EVE_TYPE_ID_MOON
            ):
                eve_moon = nearest_celestial.eve_object
                moon, _ = Moon.objects.get_or_create(eve_moon=eve_moon)
            else:
                moon = None

            eve_type, _ = EveType.objects.get_or_create_esi(
                id=structure_info["type_id"]
            )
            refinery, _ = Refinery.objects.update_or_create(
                id=refinery["structure_id"],
                defaults={
                    "name": structure_info["name"],
                    "eve_type": eve_type,
                    "moon": moon,
                    "corporation": mining_corp,
                },
            )
            user_report.append(
                {
                    "moon_name": moon.eve_moon.name if moon else "(none found)",
                    "refinery_name": refinery.name,
                }
            )

    logger.info("%s: Fetching notifications", mining_corp)
    notifications = esi.client.Character.get_characters_character_id_notifications(
        character_id=mining_corp.character.character_id,
        token=token.valid_access_token(),
    ).result()

    # add extractions for refineries if any are found
    logger.info(
        "%s: Process extraction events from %d notifications",
        mining_corp,
        len(notifications),
    )
    last_extraction_started = dict()
    moon_updated = False
    for notification in sorted(notifications, key=lambda k: k["timestamp"]):
        parsed_text = yaml.safe_load(notification["text"])
        if notification["type"] in [
            "MoonminingAutomaticFracture",
            "MoonminingExtractionCancelled",
            "MoonminingExtractionFinished",
            "MoonminingExtractionStarted",
            "MoonminingLaserFired",
        ]:
            structure_id = parsed_text["structureID"]
            try:
                refinery = Refinery.objects.get(id=structure_id)
            except Refinery.DoesNotExist:
                refinery = None
            # update the refinery's moon in case it was not found by nearest_celestial
            if refinery and not moon_updated:
                moon_updated = True
                eve_moon, _ = EveMoon.objects.get_or_create_esi(
                    id=parsed_text["moonID"]
                )
                moon, _ = Moon.objects.get_or_create(eve_moon=eve_moon)
                if refinery.moon != moon:
                    refinery.moon = moon
                    refinery.save()

            if notification["type"] == "MoonminingExtractionStarted":
                if not refinery:
                    continue  # we ignore notifications for unknown refineries
                extraction, _ = Extraction.objects.get_or_create(
                    refinery=refinery,
                    ready_time=ldap_time_2_datetime(parsed_text["readyTime"]),
                    defaults={
                        "auto_time": ldap_time_2_datetime(parsed_text["autoTime"])
                    },
                )
                last_extraction_started[id] = extraction
                ore_volume_by_type = parsed_text["oreVolumeByType"].items()
                for ore_type_id, ore_volume in ore_volume_by_type:
                    eve_type, _ = EveType.objects.get_or_create_esi(
                        id=ore_type_id,
                        enabled_sections=[EveType.Section.TYPE_MATERIALS],
                    )
                    ExtractionProduct.objects.get_or_create(
                        extraction=extraction,
                        eve_type=eve_type,
                        defaults={"volume": ore_volume},
                    )

            # remove latest started extraction if it was canceled
            # and not finished
            if notification["type"] == "MoonminingExtractionCancelled":
                if structure_id in last_extraction_started:
                    extraction = last_extraction_started[structure_id]
                    extraction.delete()

            if notification["type"] == "MoonminingExtractionFinished":
                if structure_id in last_extraction_started:
                    del last_extraction_started[structure_id]

            # TODO: add logic to handle canceled extractions


@shared_task
def update_moon_income():
    """update the income for all moons"""
    EveMarketPrice.objects.update_from_esi()
    logger.info("Re-calculating moon income for %d moons...", Moon.objects.count())
    for moon in Moon.objects.all():
        moon.update_income_estimate()
