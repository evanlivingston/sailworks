from datetime import timedelta
from dataclasses import replace
import json

import pytest

from gweatherrouting.core.forecasttranche import ModelConfiguration
from gweatherrouting.core.forecastrefreshplan import (
    MODEL_CAPABILITIES,
    RefreshCancelledError,
    plan_refresh,
)
from gweatherrouting.core.forecastvalidation import (
    ForecastValidationError,
    inspect_download,
)
from test.forecastvalidation_fixtures import (
    RUN,
    Band,
    Dataset,
    GlobalDataset,
    ProjectedDataset,
    RotatedDataset,
    band,
    dataset_for,
    inspect_fake,
    planned,
)


def test_inspects_bands_and_returns_immutable_full_per_field_coverage():
    request = planned(
        ("wind", "pressure"),
        {"wind": (0, 1, 3), "pressure": (0, 3)},
        model="HRDPS",
    )
    ds = RotatedDataset(
        [
            band("UGRD", 0, model="HRDPS"),
            band("VGRD", 0, model="HRDPS"),
            band("PRMSL", 0, model="HRDPS"),
            band("UGRD", 1, model="HRDPS"),
            band("VGRD", 1, model="HRDPS"),
            band("UGRD", 3, model="HRDPS"),
            band("VGRD", 3, model="HRDPS"),
            band("PRMSL", 3, model="HRDPS"),
        ]
    )
    result = inspect_fake("fake.grib", request, ds)
    assert result.model_id == "HRDPS"
    assert result.resolution == "0.0225deg-rotated"
    assert result.time_steps == (RUN, RUN + timedelta(hours=1), RUN.replace(hour=3))
    assert dict(result.field_times) == {
        "wind": (RUN, RUN + timedelta(hours=1), RUN.replace(hour=3)),
        "pressure": (RUN, RUN.replace(hour=3)),
    }
    with pytest.raises((AttributeError, TypeError)):
        result.field_times[0][1][0] = RUN


@pytest.mark.parametrize(
    "bands,planned_case",
    [
        ((band("UGRD", 0), band("VGRD", 0)), planned(steps={"wind": (0, 3)})),
        (
            (
                band("UGRD", 0),
                band("VGRD", 0),
                band("UGRD", 3),
                band("VGRD", 3, model="HRRR"),
            ),
            planned(),
        ),
        (
            (
                band("UGRD", 0),
                band("VGRD", 0, level="100-HTGL"),
                band("UGRD", 3),
                band("VGRD", 3),
            ),
            planned(),
        ),
        (
            (
                band("UGRD", 0, unit="knots"),
                band("VGRD", 0),
                band("UGRD", 3),
                band("VGRD", 3),
            ),
            planned(),
        ),
        ((band("UGRD", 0), band("UGRD", 3), band("VGRD", 3)), planned()),
    ],
)
def test_rejects_incomplete_or_malformed_records(bands, planned_case):
    with pytest.raises(ForecastValidationError):
        inspect_fake("fake.grib", planned_case, Dataset(bands))


def test_cancellation_and_bad_grid_are_rejected():
    with pytest.raises(RefreshCancelledError, match="cancelled"):
        inspect_fake("fake.grib", planned(), dataset_for(), lambda: True)
    broken = dataset_for()
    broken.GetGeoTransform = lambda: (0, 0, 0, 0, 0, 0)
    with pytest.raises(ForecastValidationError, match="singular"):
        inspect_fake("fake.grib", planned(), broken)


def test_cancellation_after_open_releases_dataset():
    dataset = dataset_for()
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(RefreshCancelledError):
        inspect_fake("fake.grib", planned(), dataset, cancel)
    assert dataset.closed


def test_rejects_extra_pressure_time_and_mixed_run():
    request = planned(("pressure",), {"pressure": (0,)})
    with pytest.raises(ForecastValidationError, match="unplanned"):
        inspect_fake(
            "fake.grib", request, Dataset([band("PRMSL", 0), band("PRMSL", 3)])
        )
    changed = band("VGRD", 3)
    changed["GRIB_REF_TIME"] = str(int(RUN.timestamp()) - 3600)
    with pytest.raises(ForecastValidationError, match="run times"):
        inspect_fake(
            "fake.grib",
            planned(),
            Dataset([band("UGRD", 0), band("VGRD", 0), band("UGRD", 3), changed]),
        )


def test_accepts_supported_projected_hrrr_grid():
    request = planned(model="HRRR")
    result = inspect_fake(
        "fake.grib",
        request,
        ProjectedDataset(
            [
                band("UGRD", 0, model="HRRR"),
                band("VGRD", 0, model="HRRR"),
                band("UGRD", 3, model="HRRR"),
                band("VGRD", 3, model="HRRR"),
            ]
        ),
    )
    assert result.resolution == "3km"


def test_accepts_true_rotated_hrdps_grid():
    request = planned(("pressure",), {"pressure": (0, 3)}, model="HRDPS")
    result = inspect_fake(
        "fake.grib",
        request,
        RotatedDataset(
            [band("PRMSL", 0, model="HRDPS"), band("PRMSL", 3, model="HRDPS")]
        ),
    )
    assert result.resolution == "0.0225deg-rotated"


def test_rejects_nonrotated_hrdps_and_nonmetre_hrrr_grids():
    with pytest.raises(ForecastValidationError, match="HRDPS grid"):
        inspect_fake(
            "fake.grib",
            planned(model="HRDPS"),
            Dataset(
                [
                    band("UGRD", 0, model="HRDPS"),
                    band("VGRD", 0, model="HRDPS"),
                    band("UGRD", 3, model="HRDPS"),
                    band("VGRD", 3, model="HRDPS"),
                ]
            ),
        )
    feet = ProjectedDataset(
        [
            band("UGRD", 0, model="HRRR"),
            band("VGRD", 0, model="HRRR"),
            band("UGRD", 3, model="HRRR"),
            band("VGRD", 3, model="HRRR"),
        ]
    )
    feet.srs.ImportFromEPSG(2263)
    with pytest.raises(ForecastValidationError, match="HRRR grid"):
        inspect_fake("fake.grib", planned(model="HRRR"), feet)


def test_accepts_full_longitude_gfs_grid_with_zero_to_360_axis():
    configuration = ModelConfiguration(
        "GFS", ("wind",), (-1, -180, 1, 180), "native", 3, (0, 3), 0
    )
    request = replace(planned(), configuration=configuration)
    ds = GlobalDataset(
        [band("UGRD", 0), band("VGRD", 0), band("UGRD", 3), band("VGRD", 3)]
    )
    result = inspect_fake("fake.grib", request, ds)
    assert result.region == (-1.0, -180.0, 1.0, 180.0)


def test_supported_grid_relative_vectors_and_unknown_flags():
    ds = dataset_for()
    result = inspect_fake("fake.grib", planned(), ds, flags=[True] * ds.RasterCount)
    assert result.model_id == "GFS"
    with pytest.raises(ForecastValidationError, match="vector"):
        inspect_fake("fake.grib", planned(), dataset_for(), flags=[None] * 4)
    with pytest.raises(ForecastValidationError, match="count"):
        inspect_fake("fake.grib", planned(), dataset_for(), flags=[False])


@pytest.mark.parametrize("truncated,fail_decode", [(True, False), (False, True)])
def test_rejects_undecodable_or_truncated_bands_and_closes_dataset(
    truncated, fail_decode
):
    bands = [
        Band(band("UGRD", 0), fail_decode, truncated),
        band("VGRD", 0),
        band("UGRD", 3),
        band("VGRD", 3),
    ]
    ds = Dataset(bands)
    with pytest.raises(ForecastValidationError, match="decode|truncated"):
        inspect_fake("fake.grib", planned(), ds)
    assert ds.closed


def test_gdal_timestamp_strings_and_plan_refresh_integration():
    from gweatherrouting.core.forecastrefreshplan import plan_refresh

    capability = MODEL_CAPABILITIES["GFS"]

    class Catalog:
        name = capability.provider_name

        def get_download_list(self, options):
            identifier = json.dumps(
                {
                    "source": self.name,
                    "date": "20250101",
                    "cycle": "00",
                    "hours": 3,
                    "steps": [0, 3],
                    "fields": ["wind"],
                }
            )
            return [["run", self.name, "size", "label", identifier]]

    configuration = ModelConfiguration(
        "GFS", ("wind",), (-1, -1, 1, 1), "native", 3, (), 3
    )
    plan = plan_refresh((configuration,), "GFS", factories={"GFS": Catalog})
    planned_model = plan.models[0]
    timestamp = str(int(RUN.timestamp())) + " sec UTC"
    bands = [band("UGRD", 0), band("VGRD", 0), band("UGRD", 3), band("VGRD", 3)]
    for metadata in bands:
        metadata["GRIB_REF_TIME"] = timestamp
        metadata["GRIB_FORECAST_SECONDS"] += " sec UTC"
        metadata["GRIB_VALID_TIME"] += " sec UTC"
    result = inspect_fake("fake.grib", planned_model, Dataset(bands))
    assert result.run_time == RUN


def test_mixed_production_identity_and_missing_pdt_are_rejected():
    bands = [band("UGRD", 0), band("VGRD", 0), band("UGRD", 3), band("VGRD", 3)]
    bands[-1]["GRIB_IDS"] = "CENTER=7 SUBCENTER=1"
    with pytest.raises(ForecastValidationError, match="identity"):
        inspect_fake("fake.grib", planned(), Dataset(bands))
    bands = [band("UGRD", 0), band("VGRD", 0), band("UGRD", 3), band("VGRD", 3)]
    del bands[-1]["GRIB_PDS_PDTN"]
    with pytest.raises(ForecastValidationError, match="deterministic"):
        inspect_fake("fake.grib", planned(), Dataset(bands))
