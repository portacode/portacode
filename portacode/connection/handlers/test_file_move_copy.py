import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from portacode.connection.handlers.file_handlers import FileMoveCopyHandler


class FileMoveCopyHandlerTests(unittest.TestCase):
    def setUp(self):
        self.handler = FileMoveCopyHandler(AsyncMock(), {})

    def test_moves_file_into_destination_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "note.txt"
            destination = root / "archive"
            source.write_text("hello")
            destination.mkdir()

            response = self.handler.execute({
                "source_path": str(source),
                "destination_path": str(destination),
                "operation": "move",
            })

            self.assertTrue(response["success"])
            self.assertFalse(source.exists())
            self.assertEqual((destination / "note.txt").read_text(), "hello")

    def test_copies_directory_without_removing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "assets"
            destination = root / "backup"
            source.mkdir()
            destination.mkdir()
            (source / "logo.txt").write_text("brand")

            self.handler.execute({
                "source_path": str(source),
                "destination_path": str(destination),
                "operation": "copy",
            })

            self.assertTrue(source.exists())
            self.assertEqual((destination / "assets" / "logo.txt").read_text(), "brand")

    def test_rejects_copying_folder_into_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            child = source / "child"
            child.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "into itself"):
                self.handler.execute({
                    "source_path": str(source),
                    "destination_path": str(child),
                    "operation": "copy",
                })

    def test_rejects_destination_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "one" / "same.txt"
            destination = root / "two"
            source.parent.mkdir()
            destination.mkdir()
            source.write_text("one")
            (destination / source.name).write_text("two")

            with self.assertRaisesRegex(ValueError, "already exists"):
                self.handler.execute({
                    "source_path": str(source),
                    "destination_path": str(destination),
                    "operation": "move",
                })


if __name__ == "__main__":
    unittest.main()
