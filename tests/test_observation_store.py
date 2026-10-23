from inat2exif import observation_store
from inat2exif.observation_store import ObservationStore


def test_observation_store_round_trips_and_tracks_latest_update(tmp_path) -> None:
    db_path = tmp_path / "cache" / "observations.db"
    observations = [
        {"id": 3, "updated_at": "2024-05-01T10:00:00Z", "species_guess": "Ñandú 🐦", "identifications": [{"id": 1}]},
        {"id": 1, "updated_at": "2024-06-01T09:00:00+02:00", "big": 2**70},
        {"id": 2, "updated_at": None, "time_observed_at": "2024-04-01T00:00:00Z"},
    ]
    with ObservationStore(db_path) as store:
        assert store.count() == 0
        assert store.latest_updated_at() is None
        store.upsert_many(observations)
        assert store.count() == 3
        assert store.ids() == {1, 2, 3}
        assert [obs["id"] for obs in store.load_all()] == [1, 2, 3]
        assert store.get(3) == observations[0]
        assert store.get(1)["big"] == 2**70
        assert store.latest_updated_at().isoformat() == "2024-06-01T07:00:00+00:00"

        store.upsert_many([{"id": 3, "updated_at": "2024-07-01T00:00:00Z", "species_guess": "updated"}])
        assert store.count() == 3
        assert store.get(3)["species_guess"] == "updated"

    with ObservationStore(db_path) as store:
        assert store.load_all() == [store.get(1), store.get(2), store.get(3)]


def test_observation_store_load_all_works_without_orjson(tmp_path, monkeypatch) -> None:
    observations = [{"id": index, "updated_at": "2024-05-01T10:00:00Z"} for index in range(1, 300)]
    with ObservationStore(tmp_path / "observations.db") as store:
        store.upsert_many(observations)
        monkeypatch.setattr(observation_store, "orjson", None)
        assert store.load_all() == observations
