import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.server import Library


class PreviewPriorityTest(unittest.TestCase):
    def test_large_preview_does_not_wait_for_thumbnail_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(Path(__file__).parent / 'fixtures' / '01-pattern.png',
                         root / '2026-09-20 12-00-00.png')
            library = Library(root, root / 'data')
            ident = library.photos[0]['id']
            for _ in range(6):
                self.assertTrue(library.conversions.acquire(timeout=1))
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(library.preview, ident, 1800)
                try:
                    preview = result.result(timeout=5)
                    self.assertEqual(preview.read_bytes()[:2], b'\xff\xd8')
                finally:
                    for _ in range(6):
                        library.conversions.release()

    def test_large_foreground_does_not_wait_for_prefetch_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(Path(__file__).parent / 'fixtures' / '01-pattern.png', root / 'photo.png')
            library = Library(root, root / 'data')
            for _ in range(3):
                self.assertTrue(library.large_background_conversions.acquire(timeout=1))
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(library.preview, library.photos[0]['id'], 1800)
                try:
                    self.assertEqual(result.result(timeout=5).read_bytes()[:2], b'\xff\xd8')
                finally:
                    for _ in range(3):
                        library.large_background_conversions.release()

    def test_visible_and_nearby_pools_are_independent(self):
        for blocked_pool, available_priority, slots in [
            ('nearby_conversions', 'visible', 6),
            ('conversions', 'nearby', 6),
            ('background_conversions', 'visible', 3),
            ('background_conversions', 'nearby', 3),
            ('conversions', 'background', 6),
            ('nearby_conversions', 'background', 6),
        ]:
            with self.subTest(blocked_pool=blocked_pool), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copy2(Path(__file__).parent / 'fixtures' / '01-pattern.png',
                             root / '2026-09-20 12-00-00.png')
                library = Library(root, root / 'data')
                ident = library.photos[0]['id']
                semaphore = getattr(library, blocked_pool)
                for _ in range(slots):
                    self.assertTrue(semaphore.acquire(timeout=1))
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(library.preview, ident, 480, available_priority)
                    try:
                        self.assertEqual(result.result(timeout=5).read_bytes()[:2], b'\xff\xd8')
                    finally:
                        for _ in range(slots):
                            semaphore.release()
