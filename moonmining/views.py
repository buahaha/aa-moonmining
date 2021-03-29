import datetime as dt
from collections import defaultdict
from enum import Enum

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Sum
from django.http import HttpResponseNotFound, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import format_html
from django.utils.timezone import now
from esi.decorators import token_required

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.evelinks import dotlan
from allianceauth.eveonline.models import EveCorporationInfo
from allianceauth.services.hooks import get_extension_logger
from app_utils.logging import LoggerAddTag
from app_utils.messages import messages_plus
from app_utils.views import (
    bootstrap_label_html,
    fontawesome_link_button_html,
    link_html,
    yesno_str,
)

from . import __title__, constants, helpers, tasks
from .app_settings import (
    MOONMINING_EXTRACTIONS_HOURS_UNTIL_STALE,
    MOONMINING_REPROCESSING_YIELD,
    MOONMINING_VOLUME_PER_MONTH,
)
from .forms import MoonScanForm
from .helpers import HttpResponseUnauthorized
from .models import Extraction, Moon, MoonProduct, OreRarityClass, Owner

# from django.views.decorators.cache import cache_page


logger = LoggerAddTag(get_extension_logger(__name__), __title__)


class ExtractionsCategory(str, helpers.EnumToDict, Enum):
    UPCOMING = "upcoming"
    PAST = "past"


class MoonsCategory(str, helpers.EnumToDict, Enum):
    ALL = "all_moons"
    UPLOADS = "uploads"
    OURS = "our_moons"


def moon_details_button_html(moon: Moon) -> str:
    return fontawesome_link_button_html(
        url=reverse("moonmining:moon_details", args=[moon.pk]),
        fa_code="fas fa-eye",
        tooltip="Show details in current window",
        button_type="default",
    )


def default_if_none(value, default):
    """Return given default if value is None"""
    if value is None:
        return default
    return value


@login_required
@permission_required("moonmining.basic_access")
def index(request):
    return redirect("moonmining:moons")


@login_required
@permission_required(["moonmining.extractions_access", "moonmining.basic_access"])
def extractions(request):
    context = {
        "page_title": "Extractions",
        "ExtractionsCategory": ExtractionsCategory.to_dict(),
        "reprocessing_yield": MOONMINING_REPROCESSING_YIELD * 100,
        "total_volume_per_month": MOONMINING_VOLUME_PER_MONTH / 1000000,
        "stale_hours": MOONMINING_EXTRACTIONS_HOURS_UNTIL_STALE,
    }
    return render(request, "moonmining/extractions.html", context)


@login_required
@permission_required(["moonmining.extractions_access", "moonmining.basic_access"])
def extractions_data(request, category):
    data = list()
    cutover_dt = now() - dt.timedelta(hours=MOONMINING_EXTRACTIONS_HOURS_UNTIL_STALE)
    extractions = Extraction.objects.select_related(
        "refinery",
        "refinery__moon",
        "refinery__owner",
        "refinery__owner__corporation",
        "refinery__owner__corporation__alliance",
    ).annotate(volume=Sum("products__volume"))
    if category == ExtractionsCategory.PAST:
        extractions = extractions.filter(ready_time__lt=cutover_dt)
    elif category == ExtractionsCategory.UPCOMING:
        extractions = extractions.filter(ready_time__gte=cutover_dt)
    else:
        extractions = Extraction.objects.none()
    for extraction in extractions:
        corporation_html = extraction.refinery.owner.name_html
        corporation_name = extraction.refinery.owner.name
        alliance_name = extraction.refinery.owner.alliance_name
        data.append(
            {
                "id": extraction.pk,
                "ready_time": {
                    "display": format_html(
                        "{}&nbsp;{}",
                        extraction.ready_time.strftime(constants.DATETIME_FORMAT),
                        bootstrap_label_html("Jackpot", "warning")
                        if extraction.is_jackpot
                        else "",
                    ),
                    "sort": extraction.ready_time,
                },
                "moon": str(extraction.refinery.moon),
                "corporation": {"display": corporation_html, "sort": corporation_name},
                "volume": extraction.volume,
                "value": extraction.value if extraction.value else None,
                "details": moon_details_button_html(extraction.refinery.moon),
                "corporation_name": corporation_name,
                "alliance_name": alliance_name,
                "is_jackpot_str": yesno_str(extraction.is_jackpot),
                "is_ready": extraction.ready_time <= now(),
            }
        )
    return JsonResponse(data, safe=False)


@login_required
@permission_required("moonmining.basic_access")
def moon_details(request, moon_pk: int):
    try:
        moon = Moon.objects.select_related("eve_moon").get(pk=moon_pk)
    except Moon.DoesNotExist:
        return HttpResponseNotFound()
    if not request.user.has_perm(
        "moonmining.view_all_moons"
    ) and not request.user.has_perm("moonmining.extractions_access"):
        return HttpResponseUnauthorized()

    product_rows = [
        {
            "ore_type_name": product.ore_type.name,
            "ore_type_url": product.ore_type.profile_url,
            "ore_rarity_tag": product.ore_type.rarity_class.bootstrap_tag_html,
            "image_url": product.ore_type.icon_url(constants.IconSize.MEDIUM),
            "amount": int(round(product.amount * 100)),
            "value": product.calc_value(),
        }
        for product in (
            MoonProduct.objects.select_related("ore_type", "ore_type__eve_group")
            .filter(moon=moon)
            .order_by("-ore_type__eve_group_id")
        )
    ]
    next_pull_data = None
    ppulls_data = None
    if hasattr(moon, "refinery"):
        next_pull = Extraction.objects.filter(
            refinery=moon.refinery, ready_time__gte=now()
        ).first()
        if next_pull:
            next_pull_product_rows = list()
            total_value = 0
            total_volume = 0
            for product in next_pull.products.select_related(
                "ore_type", "ore_type__eve_group"
            ).order_by("-ore_type__eve_group_id"):
                value = product.calc_value()
                total_value += default_if_none(value, 0)
                total_volume += product.volume
                ore_type = product.ore_type
                next_pull_product_rows.append(
                    {
                        "ore_type_name": ore_type.name,
                        "ore_type_url": ore_type.profile_url,
                        "ore_quality_tag": ore_type.quality_class.bootstrap_tag_html,
                        "image_url": ore_type.icon_url(constants.IconSize.SMALL),
                        "volume": product.volume,
                        "value": value,
                    }
                )
            next_pull_data = {
                "ready_time": next_pull.ready_time,
                "auto_time": next_pull.auto_time,
                "started_by": next_pull.started_by,
                "total_value": total_value,
                "total_volume": total_volume,
                "products": next_pull_product_rows,
            }
            ppulls_data = Extraction.objects.filter(
                refinery=moon.refinery, ready_time__lt=now()
            )

    context = {
        "page_title": "Moon Details",
        "moon": moon,
        "product_rows": product_rows,
        "next_pull": next_pull_data,
        "ppulls": ppulls_data,
        "reprocessing_yield": MOONMINING_REPROCESSING_YIELD * 100,
        "total_volume_per_month": MOONMINING_VOLUME_PER_MONTH / 1000000,
    }
    return render(request, "moonmining/moon_details.html", context)


@permission_required(["moonmining.basic_access", "moonmining.upload_moon_scan"])
@login_required()
def upload_survey(request):
    context = {"page_title": "Upload Moon Surveys"}
    if request.method == "POST":
        form = MoonScanForm(request.POST)
        if form.is_valid():
            scans = request.POST["scan"]
            tasks.process_survey_input.delay(scans, request.user.pk)
            messages_plus.success(
                request,
                (
                    "Your scan has been submitted for processing. You will"
                    "receive a notification once processing is complete."
                ),
            )
            return render(request, "moonmining/add_scan.html", context=context)
        else:
            messages_plus.error(
                request, "Oh No! Something went wrong with your moon scan submission."
            )
            return redirect("moonmining:moon_details")
    else:
        return render(request, "moonmining/add_scan.html", context=context)


@login_required()
@permission_required("moonmining.basic_access")
def moons(request):
    context = {
        "page_title": "Moons",
        "MoonsCategory": MoonsCategory.to_dict(),
        "reprocessing_yield": MOONMINING_REPROCESSING_YIELD * 100,
        "total_volume_per_month": MOONMINING_VOLUME_PER_MONTH / 1000000,
    }
    return render(request, "moonmining/moons.html", context)


# @cache_page(60 * 5) TODO: Remove for release
@login_required()
@permission_required("moonmining.basic_access")
def moons_data(request, category):
    """returns moon list in JSON for DataTables AJAX"""
    data = list()
    moon_query = Moon.objects.select_related(
        "eve_moon",
        "eve_moon__eve_planet__eve_solar_system",
        "eve_moon__eve_planet__eve_solar_system__eve_constellation__eve_region",
        "refinery",
        "refinery__owner",
        "refinery__owner__corporation",
        "refinery__owner__corporation__alliance",
    )
    if category == MoonsCategory.ALL and request.user.has_perm(
        "moonmining.view_all_moons"
    ):
        pass
    elif category == MoonsCategory.OURS and request.user.has_perm(
        "moonmining.extractions_access"
    ):
        moon_query = moon_query.filter(refinery__isnull=False)
    elif category == MoonsCategory.UPLOADS and request.user.has_perm(
        "moonmining.upload_moon_scan"
    ):
        moon_query = moon_query.filter(products_updated_by=request.user)
    else:
        return JsonResponse([], safe=False)

    for moon in moon_query.iterator():
        solar_system_name = moon.eve_moon.eve_planet.eve_solar_system.name
        solar_system_link = link_html(
            dotlan.solar_system_url(solar_system_name), solar_system_name
        )
        has_refinery = hasattr(moon, "refinery")
        if has_refinery:
            corporation_html = moon.refinery.owner.name_html
            corporation_name = moon.refinery.owner.name
            alliance_name = moon.refinery.owner.alliance_name
            has_details_access = request.user.has_perm(
                "moonmining.extractions_access"
            ) or request.user.has_perm("moonmining.view_all_moons")
        else:
            corporation_html = corporation_name = alliance_name = ""
            has_details_access = request.user.has_perm("moonmining.view_all_moons")
        region_name = (
            moon.eve_moon.eve_planet.eve_solar_system.eve_constellation.eve_region.name
        )
        details_html = moon_details_button_html(moon) if has_details_access else ""
        moon_data = {
            "id": moon.pk,
            "moon_name": moon.eve_moon.name,
            "corporation": {"display": corporation_html, "sort": corporation_name},
            "solar_system_link": solar_system_link,
            "region_name": region_name,
            "value": moon.value,
            "rarity_class": {
                "display": moon.rarity_tag_html,
                "sort": moon.rarity_class,
            },
            "details": details_html,
            "has_refinery_str": "yes" if has_refinery else "no",
            "solar_system_name": solar_system_name,
            "corporation_name": corporation_name,
            "alliance_name": alliance_name,
            "rarity_class_label": OreRarityClass(moon.rarity_class).label,
            "has_refinery": has_refinery,
        }
        data.append(moon_data)
    return JsonResponse(data, safe=False)


@permission_required(["moonmining.add_owner", "moonmining.basic_access"])
@token_required(scopes=Owner.esi_scopes())
@login_required
def add_owner(request, token):
    try:
        character_ownership = request.user.character_ownerships.select_related(
            "character"
        ).get(character__character_id=token.character_id)
    except CharacterOwnership.DoesNotExist:
        return HttpResponseNotFound()
    try:
        corporation = EveCorporationInfo.objects.get(
            corporation_id=character_ownership.character.corporation_id
        )
    except EveCorporationInfo.DoesNotExist:
        corporation = EveCorporationInfo.objects.create_corporation(
            corp_id=character_ownership.character.corporation_id
        )
        corporation.save()

    owner, _ = Owner.objects.update_or_create(
        corporation=corporation,
        defaults={"character_ownership": character_ownership},
    )
    tasks.update_owner.delay(owner.pk)
    messages_plus.success(request, f"Update of refineres started for {owner}.")
    return redirect("moonmining:extractions")


@login_required()
@permission_required(["moonmining.basic_access", "moonmining.reports_access"])
def reports(request):
    context = {
        "page_title": "Reports",
        "reprocessing_yield": MOONMINING_REPROCESSING_YIELD * 100,
        "total_volume_per_month": MOONMINING_VOLUME_PER_MONTH / 1000000,
    }
    return render(request, "moonmining/reports.html", context)


@login_required()
@permission_required(["moonmining.basic_access", "moonmining.reports_access"])
def report_owned_value_data(request):
    moon_query = Moon.objects.select_related(
        "eve_moon",
        "eve_moon__eve_planet__eve_solar_system",
        "eve_moon__eve_planet__eve_solar_system__eve_constellation__eve_region",
        "refinery",
        "refinery__owner",
        "refinery__owner__corporation",
        "refinery__owner__corporation__alliance",
    ).filter(refinery__isnull=False)
    corporation_moons = defaultdict(lambda: {"moons": list(), "total": 0})
    for moon in moon_query.order_by("eve_moon__name"):
        corporation_name = moon.refinery.owner.name
        corporation_moons[corporation_name]["moons"].append(moon)
        corporation_moons[corporation_name]["total"] += default_if_none(moon.value, 0)

    moon_ranks = {
        moon_pk: rank
        for rank, moon_pk in enumerate(
            moon_query.filter(value__isnull=False)
            .order_by("-value")
            .values_list("pk", flat=True)
        )
    }
    grand_total = sum(
        [corporation["total"] for corporation in corporation_moons.values()]
    )
    data = list()
    for corporation_name, details in corporation_moons.items():
        corporation = f"{corporation_name} ({len(details['moons'])})"
        counter = 0
        for moon in details["moons"]:
            grand_total_percent = (
                default_if_none(moon.value, 0) / grand_total * 100
                if grand_total > 0
                else None
            )
            rank = moon_ranks[moon.pk] + 1 if moon.pk in moon_ranks else None
            data.append(
                {
                    "corporation": corporation,
                    "moon": {"display": moon.name, "sort": counter},
                    "region": moon.region.name,
                    "rarity_class": moon.rarity_tag_html,
                    "value": moon.value,
                    "rank": rank,
                    "total": None,
                    "is_total": False,
                    "grand_total_percent": grand_total_percent,
                }
            )
            counter += 1
        data.append(
            {
                "corporation": corporation,
                "moon": {"display": "TOTAL", "sort": counter},
                "region": None,
                "rarity_class": None,
                "value": None,
                "rank": None,
                "total": details["total"],
                "is_total": True,
                "grand_total_percent": None,
            }
        )
    return JsonResponse(data, safe=False)
