"""
Pipeline orchestrator — ties all steps together end-to-end.

Flow:
1. Load config
2. Load counters.json and processed_ids.json from GitHub
3. List new images from Google Drive
4. Download & tag each image with Gemini
5. Assign deterministic IDs
6. Build WardrobeItem entries
7. Merge into wardrobe.json (no duplicates)
8. Queue all changes (images, wardrobe.json, counters.json, processed_ids.json)
9. Single GitHub commit

Idempotent: if the run fails partway, nothing already committed is reprocessed,
and nothing is double-counted. Only the final commit makes changes permanent.
"""

import json
import logging
from datetime import date, timezone
from typing import Optional

from app.config import Settings, load_settings
from app.drive_client import download_image, list_new_images
from app.gemini_tagger import tag_image
from app.github_writer import GitHubWriter
from app.id_registry import IDRegistry
from app.schemas import (
    WardrobeCatalog,
    WardrobeItem,
    catalog_to_json,
    load_catalog_from_json,
)

logger = logging.getLogger(__name__)


# File paths in the GitHub repo
_WARDROBE_JSON_PATH = "wardrobe.json"
_COUNTERS_JSON_PATH = "data/counters.json"
_PROCESSED_IDS_PATH = "data/processed_ids.json"


def _load_json_or_default(writer: GitHubWriter, path: str, default):
    """Load a JSON file from GitHub, returning default if not found."""
    content = writer.get_file_content(path)
    if content is None:
        logger.info(f"{path} not found in repo, using default: {type(default).__name__}")
        return default
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse {path}: {e}, using default")
        return default


def run_pipeline(settings: Optional[Settings] = None) -> dict:
    """
    Execute the full wardrobe pipeline.

    Args:
        settings: Pre-loaded settings. If None, loads from environment.

    Returns:
        Summary dict with keys:
        - new_images_found: int
        - items_processed: int
        - items_failed: int
        - commit_sha: str or None
        - item_details: list of dicts with per-item results
    """
    if settings is None:
        settings = load_settings()

    logger.info("=" * 60)
    logger.info("WARDROBE PIPELINE — Starting run")
    logger.info("=" * 60)

    # ─── Initialize GitHub writer ─────────────────────────────────────────
    writer = GitHubWriter(
        token=settings.github_token,
        repo_name=settings.github_repo,
        branch=settings.github_branch,
        image_assets_path=settings.image_assets_path,
    )

    try:
        # ─── Step 1: Load existing state from GitHub ──────────────────────
        logger.info("📂 Loading state from GitHub...")

        counters = _load_json_or_default(writer, _COUNTERS_JSON_PATH, {})
        processed_ids_list = _load_json_or_default(writer, _PROCESSED_IDS_PATH, [])
        processed_ids = set(processed_ids_list)

        wardrobe_json = writer.get_file_content(_WARDROBE_JSON_PATH)
        catalog = load_catalog_from_json(wardrobe_json) if wardrobe_json else WardrobeCatalog()

        registry = IDRegistry(counters)

        # Register existing IDs so we detect collisions
        for item in catalog.items:
            registry.register_existing_id(item.id)

        logger.info(
            f"State loaded: {len(catalog.items)} catalog items, "
            f"{len(processed_ids)} processed file IDs, "
            f"{sum(counters.values())} total in counters"
        )

        # ─── Step 2: List new images from Drive ───────────────────────────
        logger.info("📸 Checking Google Drive for new images...")

        new_images = list_new_images(
            folder_id=settings.drive_folder_id,
            processed_ids=processed_ids,
            service_account_info=settings.service_account_info,
        )

        if not new_images:
            logger.info("✅ No new images found. Pipeline complete.")
            return {
                "new_images_found": 0,
                "items_processed": 0,
                "items_failed": 0,
                "commit_sha": None,
                "item_details": [],
            }

        logger.info(f"Found {len(new_images)} new images to process")

        # ─── Step 3: Download and tag each image ──────────────────────────
        logger.info("🏷️  Tagging images with Gemini...")

        # Track download results separately so we can batch tag + assign
        downloaded: list[dict] = []  # {file_info, image_bytes}
        download_failures: list[dict] = []

        for img in new_images:
            try:
                image_bytes = download_image(
                    file_id=img["id"],
                    service_account_info=settings.service_account_info,
                )
                downloaded.append({"file_info": img, "image_bytes": image_bytes})
            except Exception as e:
                logger.error(f"Failed to download {img['name']}: {e}")
                download_failures.append({
                    "drive_file_id": img["id"],
                    "drive_filename": img["name"],
                    "status": "download_failed",
                    "error": str(e),
                })

        # Tag downloaded images
        tagged_results = []
        for item in downloaded:
            result = tag_image(
                image_bytes=item["image_bytes"],
                drive_file_id=item["file_info"]["id"],
                drive_filename=item["file_info"]["name"],
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
            )
            tagged_results.append({
                "tagging_result": result,
                "image_bytes": item["image_bytes"],
            })

        # ─── Step 4: Assign IDs and build catalog entries ─────────────────
        logger.info("🔢 Assigning IDs and building catalog entries...")

        new_catalog_items: list[WardrobeItem] = []
        item_details: list[dict] = []
        new_processed_ids: set[str] = set()
        items_failed = len(download_failures)

        for entry in tagged_results:
            result = entry["tagging_result"]
            image_bytes = entry["image_bytes"]
            attrs = result.attributes

            # Assign ID
            category_str = attrs.category.value
            item_id = registry.assign_id(category_str)

            # Build image URL
            image_path = f"{settings.image_assets_path}/{item_id}.png"
            image_url = writer.get_raw_url(image_path)

            # Build catalog item
            ward_item = WardrobeItem(
                id=item_id,
                category=category_str,
                color=attrs.color,
                pattern=attrs.pattern.value,
                sleeve_type=attrs.sleeve_type.value,
                fit=attrs.fit.value,
                material_guess=attrs.material_guess,
                notes=attrs.notes,
                image_url=image_url,
                date_added=date.today().isoformat(),
            )
            new_catalog_items.append(ward_item)

            # Queue image upload
            writer.queue_image(item_id, image_bytes)

            # Track as processed
            new_processed_ids.add(result.drive_file_id)

            status = "review_needed" if result.needs_manual_review else "ok"
            item_details.append({
                "id": item_id,
                "drive_file_id": result.drive_file_id,
                "drive_filename": result.drive_filename,
                "category": category_str,
                "color": attrs.color,
                "status": status,
                "review_reason": result.review_reason,
            })

            logger.info(
                f"  {result.drive_filename} → {item_id} ({category_str}) "
                f"[{'⚠️ review' if result.needs_manual_review else '✅ ok'}]"
            )

        # Add download failures to details
        for fail in download_failures:
            item_details.append({**fail, "id": None})

        # ─── Step 5: Merge into catalog ───────────────────────────────────
        logger.info("📋 Merging new items into wardrobe.json...")
        added = catalog.merge_items(new_catalog_items)
        logger.info(f"Merged {len(added)} new items (skipped {len(new_catalog_items) - len(added)} duplicates)")

        # ─── Step 6: Queue all file updates ───────────────────────────────
        logger.info("📝 Preparing commit...")

        # wardrobe.json
        writer.update_file(_WARDROBE_JSON_PATH, catalog_to_json(catalog), "")

        # counters.json
        writer.update_file(_COUNTERS_JSON_PATH, registry.counters_json(), "")

        # processed_ids.json — merge old + new
        all_processed = processed_ids | new_processed_ids
        processed_json = json.dumps(sorted(all_processed), indent=2)
        writer.update_file(_PROCESSED_IDS_PATH, processed_json, "")

        # ─── Step 7: Single commit ────────────────────────────────────────
        assigned_ids = [d["id"] for d in item_details if d.get("id")]
        commit_msg = _build_commit_message(assigned_ids, len(new_images))
        commit_sha = writer.commit_batch(commit_msg)

        logger.info("=" * 60)
        logger.info(
            f"✅ PIPELINE COMPLETE — "
            f"{len(assigned_ids)} items processed, "
            f"{items_failed} failed, "
            f"commit: {commit_sha[:8] if commit_sha else 'N/A'}"
        )
        logger.info("=" * 60)

        return {
            "new_images_found": len(new_images),
            "items_processed": len(assigned_ids),
            "items_failed": items_failed,
            "commit_sha": commit_sha,
            "item_details": item_details,
        }

    finally:
        writer.close()


def _build_commit_message(item_ids: list[str], total_photos: int) -> str:
    """
    Build a descriptive commit message.

    Args:
        item_ids: List of newly assigned item IDs.
        total_photos: Total number of photos in this run.

    Returns:
        Commit message string.
    """
    if not item_ids:
        return f"Pipeline run: {total_photos} photos processed (no new items)"

    ids_str = ", ".join(item_ids[:10])  # Cap at 10 IDs in message
    suffix = f" and {len(item_ids) - 10} more" if len(item_ids) > 10 else ""
    return f"Add items: {ids_str}{suffix} ({total_photos} photos processed)"
