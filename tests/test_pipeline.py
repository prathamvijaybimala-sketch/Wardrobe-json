"""
Comprehensive test suite for the wardrobe pipeline.

Tests each component in isolation with mocked external services,
then tests the full pipeline flow end-to-end with mocked Drive,
Gemini, and GitHub APIs.

Run with: pytest tests/ -v
"""

import json
import os
import pytest
from datetime import date
from unittest.mock import MagicMock, patch

# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_service_account():
    """Mock service account info."""
    return {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "key123",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----\n",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "client_id": "123456",
    }


@pytest.fixture
def mock_settings(sample_service_account):
    """Mock Settings with test values."""
    from app.config import Settings
    return Settings(
        drive_folder_id="test_folder_123",
        service_account_info=sample_service_account,
        gemini_api_key="test-gemini-key",
        gemini_model="gemini-2.5-flash-lite",
        github_token="ghp_test_token",
        github_repo="testuser/wardrobe-data",
        github_branch="main",
        image_assets_path="images",
    )


@pytest.fixture
def sample_drive_files():
    """Sample Drive file listing."""
    return [
        {"id": "file_001", "name": "shirt_front.png", "mimeType": "image/png"},
        {"id": "file_002", "name": "jeans_photo.png", "mimeType": "image/png"},
        {"id": "file_003", "name": "jacket_shot.png", "mimeType": "image/png"},
    ]


@pytest.fixture
def sample_image_bytes():
    """Minimal valid PNG bytes (1x1 transparent pixel)."""
    return (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
        b'\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
        b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01'
        b'\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
    )


# ─── Config Tests ────────────────────────────────────────────────────────────

class TestConfig:
    """Tests for app/config.py"""

    def test_missing_vars_fails_fast(self, monkeypatch):
        """App must not boot with missing env vars."""
        # Clear all relevant env vars
        for key in ["GOOGLE_DRIVE_FOLDER_ID", "GEMINI_API_KEY", "GITHUB_TOKEN",
                     "GITHUB_REPO", "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_B64"]:
            monkeypatch.delenv(key, raising=False)

        from app.config import load_settings
        with pytest.raises(SystemExit, match="Missing required"):
            load_settings()

    def test_invalid_github_repo_format(self, monkeypatch, tmp_path):
        """GITHUB_REPO must be owner/repo format."""
        sa_path = tmp_path / "sa.json"
        sa_path.write_text(json.dumps({"type": "service_account"}))

        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "test")
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("GITHUB_TOKEN", "test")
        monkeypatch.setenv("GITHUB_REPO", "invalid_no_slash")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(sa_path))

        from app.config import load_settings
        with pytest.raises(SystemExit, match="owner/repo"):
            load_settings()

    def test_loads_settings_from_file(self, monkeypatch, tmp_path, sample_service_account):
        """Settings load correctly from .env-style vars."""
        sa_path = tmp_path / "sa.json"
        sa_path.write_text(json.dumps(sample_service_account))

        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "folder123")
        monkeypatch.setenv("GEMINI_API_KEY", "key123")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("GITHUB_REPO", "myuser/myrepo")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(sa_path))
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
        monkeypatch.setenv("GITHUB_BRANCH", "develop")

        from app.config import load_settings
        s = load_settings()

        assert s.drive_folder_id == "folder123"
        assert s.gemini_api_key == "key123"
        assert s.github_repo == "myuser/myrepo"
        assert s.github_owner == "myuser"
        assert s.github_repo_name == "myrepo"
        assert s.gemini_model == "gemini-2.5-flash"
        assert s.github_branch == "develop"

    def test_loads_b64_service_account(self, monkeypatch):
        """Service account loads from base64 env var."""
        import base64
        sa = {"type": "service_account", "client_email": "b64@test.com"}
        b64 = base64.b64encode(json.dumps(sa).encode()).decode()

        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "test")
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("GITHUB_TOKEN", "test")
        monkeypatch.setenv("GITHUB_REPO", "user/repo")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_B64", b64)

        from app.config import load_settings
        s = load_settings()
        assert s.service_account_info["client_email"] == "b64@test.com"


# ─── Schema Tests ────────────────────────────────────────────────────────────

class TestSchemas:
    """Tests for app/schemas.py"""

    def test_garment_attributes_valid(self):
        """GarmentAttributes accepts valid enum values."""
        from app.schemas import GarmentAttributes
        attrs = GarmentAttributes(
            category="full_sleeve_shirt",
            sleeve_type="full",
            color="dark navy blue",
            pattern="solid",
            fit="regular",
            material_guess="cotton",
            notes="spread collar",
        )
        assert attrs.category.value == "full_sleeve_shirt"
        assert attrs.color == "dark navy blue"

    def test_garment_attributes_invalid_category(self):
        """GarmentAttributes rejects invalid category values."""
        from app.schemas import GarmentAttributes
        with pytest.raises(Exception):
            GarmentAttributes(
                category="invalid_category",
                sleeve_type="full",
                color="red",
                pattern="none",
                fit="regular",
                material_guess="unknown",
            )

    def test_wardrobe_catalog_merge(self):
        """merge_items appends new items, skips duplicates."""
        from app.schemas import WardrobeCatalog, WardrobeItem

        catalog = WardrobeCatalog(items=[
            WardrobeItem(
                id="FS01", category="full_sleeve_shirt", color="navy",
                pattern="solid", sleeve_type="full", fit="regular",
                material_guess="cotton", notes="", image_url="url", date_added="2026-09-01"
            ),
        ])

        new_items = [
            WardrobeItem(
                id="FS02", category="full_sleeve_shirt", color="white",
                pattern="solid", sleeve_type="full", fit="slim",
                material_guess="cotton", notes="", image_url="url2", date_added="2026-09-06"
            ),
            WardrobeItem(  # duplicate — should be skipped
                id="FS01", category="full_sleeve_shirt", color="BLACK",
                pattern="solid", sleeve_type="full", fit="regular",
                material_guess="polyester", notes="should not appear",
                image_url="url_dup", date_added="2026-09-06"
            ),
        ]

        added = catalog.merge_items(new_items)
        assert len(added) == 1
        assert added[0].id == "FS02"
        assert len(catalog.items) == 2
        # Original FS01 should be unchanged
        assert catalog.items[0].color == "navy"

    def test_catalog_json_roundtrip(self):
        """Catalog serializes and deserializes correctly."""
        from app.schemas import WardrobeCatalog, WardrobeItem, catalog_to_json, load_catalog_from_json

        catalog = WardrobeCatalog(items=[
            WardrobeItem(
                id="TR01", category="trousers", color="khaki",
                pattern="none", sleeve_type="n/a", fit="regular",
                material_guess="cotton", notes="chino",
                image_url="http://example.com/TR01.png",
                date_added="2026-09-06",
            ),
        ])

        json_str = catalog_to_json(catalog)
        loaded = load_catalog_from_json(json_str)
        assert len(loaded.items) == 1
        assert loaded.items[0].id == "TR01"
        assert loaded.items[0].image_url == "http://example.com/TR01.png"


# ─── ID Registry Tests ───────────────────────────────────────────────────────

class TestIDRegistry:
    """Tests for app/id_registry.py"""

    def test_assign_ids_from_empty(self):
        """IDs assigned sequentially from zero."""
        from app.id_registry import IDRegistry
        reg = IDRegistry({})
        assert reg.assign_id("full_sleeve_shirt") == "FS01"
        assert reg.assign_id("full_sleeve_shirt") == "FS02"
        assert reg.assign_id("jeans") == "JN01"

    def test_assign_ids_from_existing_counters(self):
        """IDs continue from existing counters."""
        from app.id_registry import IDRegistry
        reg = IDRegistry({"full_sleeve_shirt": 10, "jeans": 3})
        assert reg.assign_id("full_sleeve_shirt") == "FS11"
        assert reg.assign_id("jeans") == "JN04"

    def test_all_category_prefixes(self):
        """All categories have valid 2-letter prefixes."""
        from app.id_registry import IDRegistry, CATEGORY_PREFIX_MAP
        from app.schemas import Category

        reg = IDRegistry({})
        for cat in Category:
            prefix = CATEGORY_PREFIX_MAP[cat.value]
            assert len(prefix) == 2, f"Prefix for {cat.value} is not 2 letters: {prefix}"
            assert prefix.isalpha(), f"Prefix for {cat.value} is not alpha: {prefix}"
            item_id = reg.assign_id(cat.value)
            assert item_id.startswith(prefix)

    def test_counters_json_output(self):
        """Counters serialize to valid JSON."""
        from app.id_registry import IDRegistry
        reg = IDRegistry({"full_sleeve_shirt": 5})
        reg.assign_id("full_sleeve_shirt")
        counters_json = reg.counters_json()
        parsed = json.loads(counters_json)
        assert parsed["full_sleeve_shirt"] == 6

    def test_collision_detection(self):
        """register_existing_id prevents ID reuse."""
        from app.id_registry import IDRegistry
        reg = IDRegistry({})
        reg.register_existing_id("FS01")
        assert reg.check_collision("FS01") is True
        assert reg.check_collision("FS02") is False

    def test_from_counters_json_empty(self):
        """Handles empty or invalid JSON gracefully."""
        from app.id_registry import IDRegistry
        reg = IDRegistry.from_counters_json("{}")
        assert reg.counters == {}

        reg2 = IDRegistry.from_counters_json("invalid json")
        assert reg2.counters == {}

        reg3 = IDRegistry.from_counters_json("")
        assert reg3.counters == {}


# ─── Gemini Tagger Tests ────────────────────────────────────────────────────

class TestGeminiTagger:
    """Tests for app/gemini_tagger.py"""

    def test_tag_image_success(self, sample_image_bytes):
        """Successful tagging returns valid attributes."""
        from app.gemini_tagger import tag_image
        from app.schemas import GarmentAttributes

        # Mock the Gemini client
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "category": "full_sleeve_shirt",
            "sleeve_type": "full",
            "color": "dark grey",
            "pattern": "solid",
            "fit": "regular",
            "material_guess": "cotton blend",
            "notes": "spread collar, single chest pocket",
        })

        with patch("app.gemini_tagger.genai.Client") as MockClient:
            mock_client = MockClient.return_value
            mock_client.models.generate_content.return_value = mock_response

            result = tag_image(
                image_bytes=sample_image_bytes,
                drive_file_id="file_001",
                drive_filename="shirt.png",
                api_key="test-key",
            )

            assert result.needs_manual_review is False
            assert result.attributes.category.value == "full_sleeve_shirt"
            assert result.attributes.color == "dark grey"
            assert result.drive_filename == "shirt.png"

    def test_tag_image_validation_failure_flags_review(self, sample_image_bytes):
        """Invalid model response is flagged for manual review."""
        from app.gemini_tagger import tag_image

        # Return invalid JSON that doesn't match schema
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "category": "invalid_not_in_enum",
            "sleeve_type": "full",
            "color": "red",
            "pattern": "none",
            "fit": "regular",
            "material_guess": "unknown",
        })

        with patch("app.gemini_tagger.genai.Client") as MockClient:
            mock_client = MockClient.return_value
            mock_client.models.generate_content.return_value = mock_response

            result = tag_image(
                image_bytes=sample_image_bytes,
                drive_file_id="file_001",
                drive_filename="bad_shirt.png",
                api_key="test-key",
                max_retries=0,  # No retries for faster test
            )

            assert result.needs_manual_review is True
            assert "FAILED TO TAG" in result.attributes.notes


# ─── GitHub Writer commit_batch Regression Tests ─────────────────────────────

class TestGitHubWriterCommitBatch:
    """
    Regression tests for github_writer.py commit_batch().

    These tests exercise the real PyGithub assertion boundary for
    create_git_tree() — NOT a fully mocked GitHubWriter. If someone
    accidentally swaps InputGitTreeElement back for a plain dict,
    these tests will fail immediately.
    """

    def _make_writer(self):
        """
        Build a GitHubWriter with a mocked repo object that still runs
        the real create_git_tree assertion against the elements we pass.
        """
        from unittest.mock import MagicMock
        from github.InputGitTreeElement import InputGitTreeElement
        import github

        with patch("app.github_writer.Github") as MockGH:
            mock_repo = MagicMock()
            mock_repo.full_name = "testuser/testrepo"
            MockGH.return_value.get_repo.return_value = mock_repo

            from app.github_writer import GitHubWriter
            writer = GitHubWriter(
                token="fake-token",
                repo_name="testuser/testrepo",
                branch="main",
                image_assets_path="images",
            )

        return writer, mock_repo

    def test_commit_batch_passes_InputGitTreeElement_not_dict(self):
        """
        REGRESSION: create_git_tree() must receive InputGitTreeElement
        instances, not plain dicts. PyGithub asserts this and raises
        AssertionError on plain dicts.
        """
        from github.InputGitTreeElement import InputGitTreeElement

        writer, mock_repo = self._make_writer()

        # Set up the mock chain for a commit
        mock_ref = MagicMock()
        mock_ref.object.sha = "base-sha-123"
        mock_repo.get_git_ref.return_value = mock_ref

        mock_base_commit = MagicMock()
        mock_base_commit.tree.sha = "tree-sha-456"
        mock_repo.get_git_commit.return_value = mock_base_commit

        # Mock create_git_blob to return a fake blob with a SHA
        mock_blob = MagicMock()
        mock_blob.sha = "blob-sha-789"
        mock_repo.create_git_blob.return_value = mock_blob

        # Mock create_git_tree — but RUN THE REAL PYGITHUB ASSERTION
        # This is the critical check: if tree_elements contains dicts
        # instead of InputGitTreeElement, this will raise AssertionError.
        def real_create_git_tree(elements, base_tree=None):
            # Replicate the exact assertion from PyGithub's source
            assert all(
                isinstance(e, InputGitTreeElement) for e in elements
            ), f"Expected InputGitTreeElement, got: {[type(e).__name__ for e in elements]}"
            mock_tree = MagicMock()
            mock_tree.sha = "new-tree-sha"
            return mock_tree

        mock_repo.create_git_tree.side_effect = real_create_git_tree

        mock_new_commit = MagicMock()
        mock_new_commit.sha = "new-commit-sha-abc"
        mock_repo.create_git_commit.return_value = mock_new_commit

        # Queue a text change and an image (exercises both code paths)
        writer.update_file("wardrobe.json", '{"items": []}', "")
        writer.queue_image("FS01", b"\x89PNG\r\n fake image bytes")

        # This must NOT raise AssertionError
        sha = writer.commit_batch("Add items: FS01 (2 photos processed)")

        assert sha == "new-commit-sha-abc"
        # Verify create_git_tree was called with proper InputGitTreeElements
        call_args = mock_repo.create_git_tree.call_args
        elements = call_args[0][0]
        assert len(elements) == 2
        for elem in elements:
            assert isinstance(elem, InputGitTreeElement), (
                f"Expected InputGitTreeElement, got {type(elem).__name__}. "
                "commit_batch() is building plain dicts again!"
            )

    def test_commit_batch_element_paths_and_modes(self):
        """Verify tree elements have correct path, mode, type, and sha."""
        from github.InputGitTreeElement import InputGitTreeElement

        writer, mock_repo = self._make_writer()

        mock_ref = MagicMock()
        mock_ref.object.sha = "base-sha"
        mock_repo.get_git_ref.return_value = mock_ref

        mock_base_commit = MagicMock()
        mock_base_commit.tree.sha = "tree-sha"
        mock_repo.get_git_commit.return_value = mock_base_commit

        blob_shas = iter(["sha-text-1", "sha-text-2", "sha-bin-1"])
        mock_repo.create_git_blob.side_effect = lambda *_: MagicMock(sha=next(blob_shas))

        captured = {}

        def capture_tree(elements, base_tree=None):
            captured["elements"] = list(elements)
            return MagicMock(sha="new-tree")

        mock_repo.create_git_tree.side_effect = capture_tree
        mock_repo.create_git_commit.return_value = MagicMock(sha="commit-sha")

        writer.update_file("wardrobe.json", "{}", "")
        writer.update_file("data/counters.json", "{}", "")
        writer.queue_image("TT01", b"\x89PNG image-bytes")

        writer.commit_batch("test commit")

        elements = captured["elements"]
        assert len(elements) == 3

        # InputGitTreeElement stores attrs as name-mangled private;
        # the public interface is _identity (the dict PyGithub serializes).
        e0, e1, e2 = [e._identity for e in elements]

        # Text files
        assert e0["path"] == "wardrobe.json"
        assert e0["mode"] == "100644"
        assert e0["type"] == "blob"
        assert e0["sha"] == "sha-text-1"

        assert e1["path"] == "data/counters.json"
        assert e1["sha"] == "sha-text-2"

        # Image file
        assert e2["path"] == "images/TT01.png"
        assert e2["sha"] == "sha-bin-1"

    def test_commit_batch_empty_returns_none(self):
        """commit_batch returns None when there's nothing to commit."""
        writer, mock_repo = self._make_writer()
        assert writer.commit_batch("nothing") is None
        mock_repo.create_git_tree.assert_not_called()

    def test_commit_batch_clears_pending_after_commit(self):
        """Pending changes are cleared after a successful commit."""
        writer, mock_repo = self._make_writer()

        mock_ref = MagicMock()
        mock_ref.object.sha = "base"
        mock_repo.get_git_ref.return_value = mock_ref

        mock_base_commit = MagicMock()
        mock_base_commit.tree.sha = "tree"
        mock_repo.get_git_commit.return_value = mock_base_commit

        mock_repo.create_git_blob.return_value = MagicMock(sha="b-sha")
        mock_repo.create_git_tree.return_value = MagicMock(sha="t-sha")
        mock_repo.create_git_commit.return_value = MagicMock(sha="c-sha")

        writer.update_file("test.json", "data", "")
        assert writer.pending_count == 1

        writer.commit_batch("test")
        assert writer.pending_count == 0


# ─── Full Pipeline Integration Test ─────────────────────────────────────────

class TestPipelineIntegration:
    """
    Integration test that runs the full pipeline with mocked external services.

    Tests the complete flow:
    Drive → Gemini → ID assignment → GitHub commit
    """

    def test_full_pipeline_run(self, mock_settings, sample_drive_files, sample_image_bytes):
        """Full pipeline processes images correctly."""
        from app.pipeline import run_pipeline

        # Mock Drive client
        with patch("app.pipeline.list_new_images") as mock_list, \
             patch("app.pipeline.download_image") as mock_download, \
             patch("app.pipeline.tag_image") as mock_tag, \
             patch("app.pipeline.GitHubWriter") as MockWriter:

            # Drive returns 3 new images
            mock_list.return_value = sample_drive_files

            # Download returns sample bytes for all
            mock_download.return_value = sample_image_bytes

            # Gemini returns different categories for each image
            def tag_side_effect(image_bytes, drive_file_id, drive_filename, api_key, model):
                from app.schemas import TaggingResult, GarmentAttributes
                categories = {
                    "file_001": ("full_sleeve_shirt", "full"),
                    "file_002": ("jeans", "n/a"),
                    "file_003": ("jacket", "full"),
                }
                cat, sleeve = categories.get(drive_file_id, ("other", "n/a"))
                return TaggingResult(
                    drive_file_id=drive_file_id,
                    drive_filename=drive_filename,
                    attributes=GarmentAttributes(
                        category=cat,
                        sleeve_type=sleeve,
                        color="test color",
                        pattern="solid",
                        fit="regular",
                        material_guess="cotton",
                        notes="test",
                    ),
                    needs_manual_review=False,
                )
            mock_tag.side_effect = tag_side_effect

            # Mock GitHub writer
            mock_writer_instance = MockWriter.return_value
            mock_writer_instance.get_file_content.return_value = None  # No existing files
            mock_writer_instance.commit_batch.return_value = "abc123def456"
            mock_writer_instance.get_raw_url.return_value = "https://raw.githubusercontent.com/testuser/wardrobe-data/main/images/FS01.png"

            result = run_pipeline(mock_settings)

            # Verify results
            assert result["new_images_found"] == 3
            assert result["items_processed"] == 3
            assert result["items_failed"] == 0
            assert result["commit_sha"] == "abc123def456"
            assert len(result["item_details"]) == 3

            # Verify IDs were assigned correctly
            ids = [d["id"] for d in result["item_details"]]
            assert "FS01" in ids
            assert "JN01" in ids
            assert "JK01" in ids

            # Verify commit was called
            mock_writer_instance.commit_batch.assert_called_once()

    def test_pipeline_idempotent_no_new_images(self, mock_settings):
        """Pipeline handles 'no new images' gracefully."""
        from app.pipeline import run_pipeline

        with patch("app.pipeline.list_new_images") as mock_list, \
             patch("app.pipeline.GitHubWriter") as MockWriter:

            mock_list.return_value = []  # No new images
            mock_writer_instance = MockWriter.return_value
            mock_writer_instance.get_file_content.return_value = None

            result = run_pipeline(mock_settings)

            assert result["new_images_found"] == 0
            assert result["items_processed"] == 0
            assert result["commit_sha"] is None

    def test_pipeline_preserves_existing_catalog(self, mock_settings, sample_image_bytes):
        """Pipeline doesn't overwrite existing catalog items."""
        from app.pipeline import run_pipeline

        existing_catalog = {
            "items": [
                {
                    "id": "FS01",
                    "category": "full_sleeve_shirt",
                    "color": "navy",
                    "pattern": "solid",
                    "sleeve_type": "full",
                    "fit": "regular",
                    "material_guess": "cotton",
                    "notes": "existing",
                    "image_url": "http://example.com/FS01.png",
                    "date_added": "2026-09-01",
                }
            ]
        }

        with patch("app.pipeline.list_new_images") as mock_list, \
             patch("app.pipeline.download_image") as mock_download, \
             patch("app.pipeline.tag_image") as mock_tag, \
             patch("app.pipeline.GitHubWriter") as MockWriter:

            # One new image (a shirt — would get FS02 since FS01 exists)
            mock_list.return_value = [{"id": "file_new", "name": "new_shirt.png", "mimeType": "image/png"}]
            mock_download.return_value = sample_image_bytes

            from app.schemas import TaggingResult, GarmentAttributes
            mock_tag.return_value = TaggingResult(
                drive_file_id="file_new",
                drive_filename="new_shirt.png",
                attributes=GarmentAttributes(
                    category="full_sleeve_shirt",
                    sleeve_type="full",
                    color="white",
                    pattern="solid",
                    fit="slim",
                    material_guess="cotton",
                    notes="new",
                ),
                needs_manual_review=False,
            )

            mock_writer_instance = MockWriter.return_value

            # Return existing catalog when wardrobe.json is read
            def get_file_side_effect(path):
                if path == "wardrobe.json":
                    return json.dumps(existing_catalog)
                elif path == "data/counters.json":
                    return json.dumps({"full_sleeve_shirt": 1})
                elif path == "data/processed_ids.json":
                    return json.dumps(["file_old"])
                return None

            mock_writer_instance.get_file_content.side_effect = get_file_side_effect
            mock_writer_instance.commit_batch.return_value = "new_commit_sha"
            mock_writer_instance.get_raw_url.return_value = "http://example.com/FS02.png"

            result = run_pipeline(mock_settings)

            assert result["items_processed"] == 1
            # The new item should get FS02, not FS01
            assert result["item_details"][0]["id"] == "FS02"

    def test_pipeline_handles_download_failure(self, mock_settings):
        """Pipeline continues when individual downloads fail."""
        from app.pipeline import run_pipeline

        with patch("app.pipeline.list_new_images") as mock_list, \
             patch("app.pipeline.download_image") as mock_download, \
             patch("app.pipeline.GitHubWriter") as MockWriter:

            mock_list.return_value = [
                {"id": "file_ok", "name": "ok.png", "mimeType": "image/png"},
                {"id": "file_fail", "name": "fail.png", "mimeType": "image/png"},
            ]

            def download_side_effect(file_id, service_account_info):
                if file_id == "file_fail":
                    raise Exception("Network error")
                return b'\x89PNG'  # minimal bytes

            mock_download.side_effect = download_side_effect

            mock_writer_instance = MockWriter.return_value
            mock_writer_instance.get_file_content.return_value = None
            mock_writer_instance.commit_batch.return_value = "partial_commit"
            mock_writer_instance.get_raw_url.return_value = "http://example.com/FS01.png"

            with patch("app.pipeline.tag_image") as mock_tag:
                from app.schemas import TaggingResult, GarmentAttributes
                mock_tag.return_value = TaggingResult(
                    drive_file_id="file_ok",
                    drive_filename="ok.png",
                    attributes=GarmentAttributes(
                        category="full_sleeve_shirt",
                        sleeve_type="full",
                        color="blue",
                        pattern="solid",
                        fit="regular",
                        material_guess="cotton",
                        notes="",
                    ),
                    needs_manual_review=False,
                )

                result = run_pipeline(mock_settings)

                assert result["items_processed"] == 1
                assert result["items_failed"] == 1
