from tools.prepare_derived_data_reinitialize import published_lineage_counts


def test_unpublished_derived_assets_do_not_block_approved_reset() -> None:
    assert published_lineage_counts(publishing_records=0, performance_records=0) == {}


def test_published_or_performance_lineage_still_blocks_reset() -> None:
    assert published_lineage_counts(publishing_records=2, performance_records=0) == {
        "publishingRecords": 2
    }
    assert published_lineage_counts(publishing_records=0, performance_records=3) == {
        "performanceRecords": 3
    }
    assert published_lineage_counts(publishing_records=2, performance_records=3) == {
        "publishingRecords": 2,
        "performanceRecords": 3,
    }
