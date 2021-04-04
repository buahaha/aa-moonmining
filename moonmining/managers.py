from collections import namedtuple
from concurrent import futures
from typing import Tuple

from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models import Sum
from django.utils.timezone import now
from eveuniverse.managers import EveTypeManager
from eveuniverse.models import EveMoon

from allianceauth.notifications import notify
from allianceauth.services.hooks import get_extension_logger
from app_utils.logging import LoggerAddTag

from . import __title__, constants

MAX_THREAD_WORKERS = 20
logger = LoggerAddTag(get_extension_logger(__name__), __title__)

SurveyProcessResult = namedtuple(
    "SurveyProcessResult", ["moon_name", "success", "error_name"]
)


class EveOreTypeManger(EveTypeManager):
    def get_queryset(self):
        """Return ore types only."""
        return (
            super()
            .get_queryset()
            .select_related("eve_group")
            .filter(published=True)
            .filter(eve_group__eve_category_id=constants.EVE_CATEGORY_ID_ASTEROID)
        )


class UpdateCalculatedPropertiesMixin:
    """Mixin for updating all calculated properties of a query set"""

    def update_calculated_properties(self):
        obj_pks = self.values_list("pk", flat=True)
        logger.info(
            "Updating calculated properties for %d %ss ...",
            len(obj_pks),
            self.model.__name__.lower(),
        )
        with futures.ThreadPoolExecutor(max_workers=MAX_THREAD_WORKERS) as executor:
            executor.map(self._thread_update_obj, list(obj_pks))
        logger.info("Completed calculating properties.")

    def _thread_update_obj(self, pk):
        logger.info(
            "Updating calculated properties for %s %d...", self.model.__name__, pk
        )
        obj = self.get(pk=pk)
        obj.update_calculated_properties()


class MoonQuerySet(models.QuerySet, UpdateCalculatedPropertiesMixin):
    pass


class MoonManager(models.Manager):
    def get_queryset(self) -> models.QuerySet:
        return MoonQuerySet(self.model, using=self._db)

    def selected_related_defaults(self) -> models.QuerySet:
        return self.select_related(
            "eve_moon",
            "eve_moon__eve_planet__eve_solar_system",
            "eve_moon__eve_planet__eve_solar_system__eve_constellation__eve_region",
            "refinery",
            "refinery__eve_type",
            "refinery__owner",
            "refinery__owner__corporation",
            "refinery__owner__corporation__alliance",
        )

    def update_moons_from_survey(self, scans: str, user: User = None) -> bool:
        """Update moons from survey input.

        Args:
            scans: raw text input from user containing moon survey data
            user: (optional) user who submitted the data
        """
        surveys, error_name = self._parse_scans(scans)
        if surveys:
            process_results, success = self._process_surveys(surveys, user)
        else:
            process_results = None
            success = False

        if user:
            success = self._send_survey_process_report_to_user(
                process_results, error_name, user, success
            )
        return success

    @staticmethod
    def _parse_scans(scans: str) -> tuple:
        surveys = []
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

        else:
            error_name = ""
        return surveys, error_name

    def _process_surveys(self, surveys: list, user: User) -> Tuple[list, bool]:
        from .models import EveOreType, MoonProduct

        overall_success = True
        process_results = list()
        for survey in surveys:
            moon_name = ""
            try:
                moon_name = survey[0][0]
                moon_id = survey[1][6]
                eve_moon, _ = EveMoon.objects.get_or_create_esi(id=moon_id)
                moon, _ = self.get_or_create(eve_moon=eve_moon)
                moon.products_updated_by = user
                moon.products_updated_at = now()
                moon_products = list()
                survey = survey[1:]
                for product_data in survey:
                    # Trim off the empty index at the front
                    product_data = product_data[1:]
                    ore_type, _ = EveOreType.objects.get_or_create_esi(
                        id=product_data[2]
                    )
                    moon_products.append(
                        MoonProduct(
                            moon=moon, amount=product_data[1], ore_type=ore_type
                        )
                    )

                with transaction.atomic():
                    moon.products.all().delete()
                    MoonProduct.objects.bulk_create(moon_products, batch_size=500)

                moon.update_calculated_properties()
                logger.info("Added moon survey for %s", moon.eve_moon.name)

            except Exception as ex:
                logger.warning(
                    "An issue occurred while processing the following moon survey: %s",
                    survey,
                    exc_info=True,
                )
                error_name = type(ex).__name__
                overall_success = success = False
            else:
                success = True
                error_name = None

            process_results.append(
                SurveyProcessResult(
                    moon_name=moon_name, success=success, error_name=error_name
                )
            )
        return process_results, overall_success

    @staticmethod
    def _send_survey_process_report_to_user(
        process_results: list, error_name: str, user: User, success: bool
    ) -> bool:
        message = "We have completed processing your moon survey input:\n\n"
        if process_results:
            for num, process_result in enumerate(process_results):
                moon_name = process_result.moon_name
                if process_result.success:
                    status = "OK"
                    error_name = ""
                else:
                    status = "FAILED"
                    success = False
                    error_name = "- {}".format(process_result.error_name)
                message += "#{}: {}: {} {}\n".format(
                    num + 1, moon_name, status, error_name
                )
        else:
            message += "\nProcessing failed"

        notify(
            user=user,
            title="Moon survey input processing results: {}".format(
                "OK" if success else "FAILED"
            ),
            message=message,
            level="success" if success else "danger",
        )
        return success


class ExtractionQuerySet(models.QuerySet, UpdateCalculatedPropertiesMixin):
    def selected_related_defaults(self) -> models.QuerySet:
        return self.select_related(
            "refinery",
            "refinery__moon",
            "refinery__moon__eve_moon",
            "refinery__owner",
            "refinery__owner__corporation",
            "refinery__owner__corporation__alliance",
            "canceled_by",
            "fractured_by",
            "started_by",
        )


class ExtractionManager(models.Manager):
    def annotate_volume(self) -> models.QuerySet:
        """Add volume of all products"""
        return self.annotate(volume=Sum("products__volume"))

    def get_queryset(self) -> models.QuerySet:
        return ExtractionQuerySet(self.model, using=self._db)