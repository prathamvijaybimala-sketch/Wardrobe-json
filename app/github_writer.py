"""
GitHub writer — commit images + wardrobe.json to the data repo.

Uses PyGithub to interact with the GitHub API via a personal access token.
Supports:
- Reading/updating JSON files (wardrobe.json, counters.json, processed_ids.json)
- Uploading binary images (renamed PNGs)
- Batching changes into a single commit per pipeline run
- Creating files that don't exist yet (first-ever run)
"""

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from github import Github, GithubException
from github.ContentFile import ContentFile
from github.InputGitTreeElement import InputGitTreeElement
from github.Repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class PendingChange:
    """A file change to be committed in the next batch."""

    path: str
    content: bytes  # UTF-8 text or binary
    is_binary: bool = False


class GitHubWriter:
    """
    Manages reading and writing files in the GitHub data repo.

    Supports batched commits: multiple file changes can be queued and
    committed together in a single GitHub commit.
    """

    def __init__(
        self,
        token: str,
        repo_name: str,
        branch: str = "main",
        image_assets_path: str = "images",
    ):
        """
        Initialize the GitHub writer.

        Args:
            token: GitHub personal access token (repo scope).
            repo_name: Repository in "owner/repo" format.
            branch: Branch to commit to.
            image_assets_path: Directory for image files within the repo.
        """
        self._gh = Github(token)
        self._repo: Repository = self._gh.get_repo(repo_name)
        self._branch = branch
        self._image_assets_path = image_assets_path
        self._pending_changes: list[PendingChange] = []

        logger.info(f"GitHubWriter initialized for {repo_name}/{branch}")

    def _get_ref(self):
        """Get the Git reference for the configured branch."""
        return self._repo.get_git_ref(f"heads/{self._branch}")

    def get_file_content(self, path: str) -> Optional[str]:
        """
        Read a text file from the repo.

        Args:
            path: File path within the repo (e.g. "wardrobe.json").

        Returns:
            File content as string, or None if the file doesn't exist.
        """
        try:
            content_file = self._repo.get_contents(path, ref=self._branch)
            if isinstance(content_file, list):
                raise ValueError(f"Path '{path}' is a directory, not a file")
            return content_file.decoded_content.decode("utf-8")
        except GithubException as e:
            if e.status == 404:
                logger.info(f"File not found in repo: {path} (will be created)")
                return None
            raise

    def get_file_sha(self, path: str) -> Optional[str]:
        """
        Get the SHA of a file in the repo (for updates).

        Args:
            path: File path within the repo.

        Returns:
            SHA string, or None if the file doesn't exist.
        """
        try:
            content_file = self._repo.get_contents(path, ref=self._branch)
            if isinstance(content_file, list):
                return None
            return content_file.sha
        except GithubException as e:
            if e.status == 404:
                return None
            raise

    def update_file(self, path: str, content: str, message: str) -> None:
        """
        Create or update a text file in the repo.

        If the file doesn't exist, it's created. If it exists, it's updated.
        The change is queued for the next batch commit.

        Args:
            path: File path within the repo.
            content: Text content to write.
            message: Commit message (used if committing immediately).
        """
        self._pending_changes.append(
            PendingChange(
                path=path,
                content=content.encode("utf-8"),
                is_binary=False,
            )
        )
        logger.info(f"Queued update for {path} ({len(content)} bytes)")

    def queue_image(self, item_id: str, image_bytes: bytes) -> str:
        """
        Queue a renamed image for upload.

        Args:
            item_id: The assigned item ID (e.g. "FS04").
            image_bytes: Raw PNG image data.

        Returns:
            The path where the image will be stored in the repo.
        """
        filename = f"{item_id}.png"
        path = f"{self._image_assets_path}/{filename}"
        self._pending_changes.append(
            PendingChange(
                path=path,
                content=image_bytes,
                is_binary=True,
            )
        )
        logger.info(f"Queued image upload: {path} ({len(image_bytes)} bytes)")
        return path

    def commit_batch(self, message: str) -> Optional[str]:
        """
        Commit all pending changes in a single GitHub commit.

        Uses the GitHub Trees API to create a new tree with all changes,
        then creates a commit and updates the branch ref.

        Args:
            message: Commit message (e.g. "Add items: FS04, TR02 (3 photos processed)").

        Returns:
            The commit SHA, or None if there were no changes to commit.
        """
        if not self._pending_changes:
            logger.info("No pending changes to commit")
            return None

        logger.info(
            f"Committing {len(self._pending_changes)} changes: {message}"
        )

        # Get the current branch HEAD
        ref = self._get_ref()
        base_sha = ref.object.sha
        base_commit = self._repo.get_git_commit(base_sha)
        base_tree_sha = base_commit.tree.sha

        # Create blobs for each file
        tree_elements = []
        for change in self._pending_changes:
            if change.is_binary:
                # Binary content — base64 encode for the blob
                encoded = base64.b64encode(change.content).decode("ascii")
                blob = self._repo.create_git_blob(encoded, "base64")
            else:
                # Text content
                blob = self._repo.create_git_blob(
                    change.content.decode("utf-8"), "utf-8"
                )

            tree_elements.append(
                InputGitTreeElement(
                    path=change.path,
                    mode="100644",
                    type="blob",
                    sha=blob.sha,
                )
            )

        # Create a new tree
        new_tree = self._repo.create_git_tree(tree_elements, base_tree=base_commit.tree)

        # Create the commit
        new_commit = self._repo.create_git_commit(
            message=message,
            tree=new_tree,
            parents=[base_commit],
        )

        # Update the branch reference
        ref.edit(new_commit.sha)

        commit_sha = new_commit.sha
        logger.info(f"✅ Committed: {commit_sha[:8]} — {message}")

        # Clear pending changes
        self._pending_changes.clear()

        return commit_sha

    @property
    def pending_count(self) -> int:
        """Number of changes queued for the next commit."""
        return len(self._pending_changes)

    @property
    def image_base_url(self) -> str:
        """Base URL for images in the repo on raw.githubusercontent.com."""
        owner, repo = self._repo.full_name.split("/")
        return (
            f"https://raw.githubusercontent.com/"
            f"{owner}/{repo}/{self._branch}/{self._image_assets_path}"
        )

    def get_raw_url(self, path: str) -> str:
        """
        Build a raw.githubusercontent.com URL for a file.

        Args:
            path: File path within the repo.

        Returns:
            Full raw URL.
        """
        owner, repo = self._repo.full_name.split("/")
        return (
            f"https://raw.githubusercontent.com/"
            f"{owner}/{repo}/{self._branch}/{path}"
        )

    def close(self) -> None:
        """Close the GitHub client connection."""
        self._gh.close()
