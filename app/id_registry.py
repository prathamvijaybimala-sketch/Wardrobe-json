"""
ID registry — deterministic ID generation from category prefix + counter.

Maintains a mapping of clothing categories to 2-letter prefixes and a running
counter per category. Counters are loaded from GitHub (counters.json) at the
start of each run, so state is never lost between Railway restarts.

ID format: <PREFIX><zero-padded number>
Example: FS04 (Full Sleeve shirt #4), TR02 (Trousers #2)
"""

import json
import logging
from typing import Optional

from app.schemas import Category

logger = logging.getLogger(__name__)


# ─── Category → Prefix Mapping ───────────────────────────────────────────────

CATEGORY_PREFIX_MAP: dict[str, str] = {
    Category.FULL_SLEEVE_SHIRT.value: "FS",
    Category.HALF_SLEEVE_SHIRT.value: "HS",
    Category.T_SHIRT.value: "TT",
    Category.POLO.value: "PO",
    Category.TANK_TOP.value: "TK",
    Category.JEANS.value: "JN",
    Category.TROUSERS.value: "TR",
    Category.SHORTS.value: "SH",
    Category.JACKET.value: "JK",
    Category.BLAZER.value: "BZ",
    Category.HOODIE.value: "HD",
    Category.SWEATSHIRT.value: "SW",
    Category.DRESS.value: "DR",
    Category.SKIRT.value: "SK",
    Category.OTHER.value: "OT",
}


class IDRegistry:
    """
    Manages per-category counters and generates deterministic IDs.

    Counters are loaded from counters.json (stored in the GitHub data repo).
    After assigning IDs, the updated counters are returned for committing back
    to GitHub.
    """

    def __init__(self, counters: dict[str, int]):
        """
        Initialize the registry with current counters.

        Args:
            counters: Dict mapping category string → current count (int).
                      e.g. {"full_sleeve_shirt": 3, "jeans": 1}
        """
        self._counters = dict(counters)
        self._assigned_ids: set[str] = set()

    @classmethod
    def from_counters_json(cls, counters_json: str) -> "IDRegistry":
        """
        Create an IDRegistry from a counters.json string.

        Args:
            counters_json: JSON string of {category: count} pairs.
                           If empty or invalid, starts from empty counters.

        Returns:
            IDRegistry instance.
        """
        try:
            counters = json.loads(counters_json)
            if not isinstance(counters, dict):
                logger.warning("counters.json is not a dict, starting fresh")
                counters = {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("Could not parse counters.json, starting fresh")
            counters = {}

        # Ensure all values are ints
        counters = {k: int(v) for k, v in counters.items()}
        return cls(counters)

    def get_prefix(self, category: str) -> str:
        """
        Get the 2-letter prefix for a category.

        Args:
            category: Category string value (e.g. "full_sleeve_shirt").

        Returns:
            2-letter prefix (e.g. "FS").

        Raises:
            ValueError: If the category is not in the mapping.
        """
        prefix = CATEGORY_PREFIX_MAP.get(category)
        if prefix is None:
            raise ValueError(
                f"Unknown category '{category}'. "
                f"Known categories: {list(CATEGORY_PREFIX_MAP.keys())}"
            )
        return prefix

    def assign_id(self, category: str) -> str:
        """
        Assign a deterministic ID for a garment of the given category.

        Increments the counter for the category and returns the new ID.
        Checks for collisions against previously assigned IDs in this run.

        Args:
            category: Category string value (e.g. "full_sleeve_shirt").

        Returns:
            Deterministic ID string (e.g. "FS04").

        Raises:
            ValueError: If the category is unknown.
        """
        prefix = self.get_prefix(category)

        # Get current count and increment
        current_count = self._counters.get(category, 0)
        new_count = current_count + 1
        self._counters[category] = new_count

        # Generate ID: prefix + zero-padded number (2 digits minimum)
        item_id = f"{prefix}{new_count:02d}"

        # Check for collisions within this run
        if item_id in self._assigned_ids:
            logger.warning(
                f"ID collision detected: {item_id} (category={category}). "
                f"This shouldn't happen — counters may be out of sync."
            )
            # Try the next number as fallback
            new_count += 1
            self._counters[category] = new_count
            item_id = f"{prefix}{new_count:02d}"

        self._assigned_ids.add(item_id)

        logger.info(
            f"Assigned ID {item_id} for category '{category}' "
            f"(counter now: {new_count})"
        )
        return item_id

    def register_existing_id(self, item_id: str) -> None:
        """
        Register an ID that already exists in the wardrobe catalog.

        Used to detect collisions when the catalog already contains items
        that were committed in previous runs.

        Args:
            item_id: Existing item ID (e.g. "FS04").
        """
        self._assigned_ids.add(item_id)

    def check_collision(self, item_id: str) -> bool:
        """
        Check if an ID already exists (was assigned this run or registered).

        Args:
            item_id: The ID to check.

        Returns:
            True if the ID is already taken.
        """
        return item_id in self._assigned_ids

    @property
    def counters(self) -> dict[str, int]:
        """
        Get the current counters dict (for saving back to GitHub).

        Returns:
            Dict mapping category → current count.
        """
        return dict(self._counters)

    def counters_json(self) -> str:
        """
        Serialize counters to JSON string (for GitHub commit).

        Returns:
            JSON string of counters.
        """
        return json.dumps(self._counters, indent=2, sort_keys=True)

    @property
    def assigned_ids(self) -> set[str]:
        """Get all IDs assigned during this run."""
        return set(self._assigned_ids)
