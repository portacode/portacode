from django.test import SimpleTestCase
from django.urls import reverse


class TreatsViewTests(SimpleTestCase):
    def test_homepage_loads(self):
        response = self.client.get(reverse("treats:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cosmic Cupcake Stand")

    def test_menu_lists_all_items(self):
        response = self.client.get("/")
        for flavor in ["Solar Flare", "Nebula Crunch", "Lunar Latte", "Comet Confetti"]:
            with self.subTest(flavor=flavor):
                self.assertContains(response, flavor)
