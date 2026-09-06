"""
Gemini image tagger — converts clothing photos into structured JSON attributes.

Uses the google-genai SDK with structured output (JSON schema mode) to guarantee
valid responses. Validates against Pydantic schemas and retries once on failure.
"""

import logging
from typing import Optional

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.schemas import GarmentAttributes, TaggingResult

logger = logging.getLogger(__name__)

# Prompt sent to Gemini alongside each image
_TAGGING_PROMPT = """You are a fashion cataloging assistant. Analyze this clothing photo and extract structured attributes.

IMPORTANT RULES:
- Only use values from the provided enums for category, sleeve_type, fit, and pattern.
- Be specific about color (e.g. "dark navy blue" not just "blue", "charcoal grey" not just "grey").
- For material_guess, provide your best educated guess based on visual cues (weave, sheen, drape).
- For notes, mention distinctive features: collar style, pocket count, logos, buttons, zippers, distressing, etc.
- If the fit is not clearly visible in the photo, use "not_visible".
- Do NOT generate any ID — that is handled separately.

Return the attributes as valid JSON matching the provided schema."""


def _create_gemini_client(api_key: str) -> genai.Client:
    """Create a Gemini API client."""
    return genai.Client(api_key=api_key)


def tag_image(
    image_bytes: bytes,
    drive_file_id: str,
    drive_filename: str,
    api_key: str,
    model: str = "gemini-2.5-flash-lite",
    max_retries: int = 1,
) -> TaggingResult:
    """
    Send an image to Gemini and get structured garment attributes.

    Uses structured output mode (JSON schema) to guarantee valid JSON responses.
    Validates the response against the Pydantic schema.

    Args:
        image_bytes: Raw PNG image data.
        drive_file_id: Source Drive file ID (for logging/tracking).
        drive_filename: Original Drive filename (for logging).
        api_key: Gemini API key.
        model: Gemini model name (default: gemini-2.5-flash-lite).
        max_retries: Number of retries on validation failure (default: 1).

    Returns:
        TaggingResult with extracted attributes or flagged for manual review.
    """
    client = _create_gemini_client(api_key)

    # Build the image part for the Gemini request
    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type="image/png",
    )

    last_error = None

    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=[image_part, _TAGGING_PROMPT],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": GarmentAttributes,
                },
            )

            # Parse the structured response
            attributes = GarmentAttributes.model_validate_json(response.text)

            logger.info(
                f"Tagged {drive_filename}: category={attributes.category.value}, "
                f"color={attributes.color}, pattern={attributes.pattern.value}"
            )

            return TaggingResult(
                drive_file_id=drive_file_id,
                drive_filename=drive_filename,
                attributes=attributes,
                needs_manual_review=False,
            )

        except ValidationError as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(
                    f"Gemini response failed validation for {drive_filename} "
                    f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
            else:
                logger.error(
                    f"Gemini response failed validation for {drive_filename} "
                    f"after {max_retries + 1} attempts: {e}"
                )

        except Exception as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(
                    f"Gemini API error for {drive_filename} "
                    f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
            else:
                logger.error(
                    f"Gemini API error for {drive_filename} "
                    f"after {max_retries + 1} attempts: {e}"
                )

    # All retries exhausted — flag for manual review
    logger.error(f"Flagging {drive_filename} for manual review due to: {last_error}")
    return TaggingResult(
        drive_file_id=drive_file_id,
        drive_filename=drive_filename,
        attributes=GarmentAttributes(
            category="other",
            sleeve_type="n/a",
            color="unknown",
            pattern="none",
            fit="not_visible",
            material_guess="unknown",
            notes=f"FAILED TO TAG: {last_error}",
        ),
        needs_manual_review=True,
        review_reason=str(last_error),
    )


def tag_images_batch(
    images: list[dict],
    download_func,
    api_key: str,
    model: str = "gemini-2.5-flash-lite",
) -> list[TaggingResult]:
    """
    Tag a batch of images from Drive.

    Args:
        images: List of dicts with keys: id, name (from Drive file listing).
        download_func: Callable that takes a file_id and returns image bytes.
        api_key: Gemini API key.
        model: Gemini model name.

    Returns:
        List of TaggingResult objects.
    """
    results = []
    for img in images:
        logger.info(f"Processing: {img['name']} ({img['id']})")

        # Download the image
        try:
            image_bytes = download_func(img["id"])
        except Exception as e:
            logger.error(f"Failed to download {img['name']}: {e}")
            results.append(
                TaggingResult(
                    drive_file_id=img["id"],
                    drive_filename=img["name"],
                    attributes=GarmentAttributes(
                        category="other",
                        sleeve_type="n/a",
                        color="unknown",
                        pattern="none",
                        fit="not_visible",
                        material_guess="unknown",
                        notes=f"DOWNLOAD FAILED: {e}",
                    ),
                    needs_manual_review=True,
                    review_reason=f"Download failed: {e}",
                )
            )
            continue

        # Tag with Gemini
        result = tag_image(
            image_bytes=image_bytes,
            drive_file_id=img["id"],
            drive_filename=img["name"],
            api_key=api_key,
            model=model,
        )
        results.append(result)

    return results
