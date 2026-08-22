import asyncio
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")

from app.hindi_provider import HindiAnimeResult, HindiProvider
from app.resolver import AniCliResolver


class FakeHindiProvider:
    async def search(self, query: str, limit: int):
        return [HindiAnimeResult(1, "direct", "direct:naruto", "Naruto Hindi")]

    async def get_episode_count(self, provider_id: str) -> int:
        return 12

    async def resolve_episode(self, provider_id: str, episode: int) -> str:
        return "https://stream.example/naruto-3.m3u8"


class HindiProviderTests(unittest.TestCase):
    def test_parse_and_rank_servers(self):
        provider = HindiProvider()
        parsed = provider._parse_js_object(
            '{"Filemoon": [{name: "S01E03", url: "https://filemoon.example/e3"}]}'
        )
        self.assertEqual(parsed["Filemoon"][0]["name"], "S01E03")

        ranked = provider._rank_servers([
            {"name": "Servabyss", "url": "https://bad.example"},
            {"name": "Vidgroud", "url": "https://good.example"},
            {"name": "Filemoon", "url": "https://best.example"},
        ])
        self.assertEqual(ranked[0]["name"], "Filemoon")
        self.assertTrue(all("abyss" not in item["name"].lower() for item in ranked))

    def test_direct_parser_preserves_seasons_and_skips_english(self):
        self.assertEqual(HindiProvider._direct_season_episode("S1EP01"), (1, 1))
        self.assertEqual(HindiProvider._direct_season_episode("S2EP12"), (2, 12))
        self.assertEqual(HindiProvider._direct_season_episode("Start Season 2 Episode-1"), (2, 1))
        self.assertEqual(
            HindiProvider._direct_season_episode(
                "Episode 1",
                "https://stream.example/Title_S02E01_1080p.mkv",
            ),
            (2, 1),
        )

        async def run():
            provider = HindiProvider(provider_order=("direct",))

            async def html(_url, referer):
                return """
                <h1 class='entry-title'>Test Anime</h1>
                <script>
                const serverVideos = {
                  filemoon: [
                    {name: "S1EP01", url: "https://stream.example/s1e1"},
                    {name: "S2EP01", url: "https://stream.example/s2e1"}
                  ],
                  english: [
                    {name: "S1EP01", url: "https://stream.example/english"}
                  ]
                };
                </script>
                """

            provider._text = html
            data = await provider._direct_anime("test-anime")
            self.assertEqual([item["number"] for item in data["episodes"]], [1, 2])
            self.assertEqual(data["episodes"][1]["season"], 2)
            self.assertEqual(
                await provider.get_episode_count("direct:test-anime"),
                2,
            )
            self.assertEqual(
                await provider.resolve_episode("direct:test-anime", 2),
                "https://stream.example/s2e1",
            )

        asyncio.run(run())

    def test_animesky_flattens_regional_seasons_and_excludes_subtitles(self):
        async def run():
            provider = HindiProvider(provider_order=("animesky",))

            async def html(_url, referer):
                return """
                <a class="season-btn" data-season="1">
                  <span class="season-label">Season 1</span>
                  <span class="season-episodes">1-2 (2)</span>
                </a>
                <a class="season-btn" data-season="2">
                  <span class="season-label">Season 2</span>
                  <span class="season-episodes">1-2 (2)</span>
                </a>
                <a class="season-btn non-regional" data-season="3">
                  <span class="season-label">Season 3</span>
                  <span class="season-episodes">1-2 (2)</span>
                </a>
                """

            provider._text = html
            self.assertEqual(
                await provider.get_episode_count("animesky:test-anime"),
                4,
            )
            self.assertEqual(
                await provider.resolve_episode("animesky:test-anime", 3),
                "https://animesky.top/episode/test-anime-2x1/",
            )

        asyncio.run(run())

    def test_desidubanime_season_selection_and_embed_decode(self):
        import base64

        seasons = [
            {"season_id": "564", "season_name": "Naruto"},
            {"season_id": "1268", "season_name": "Naruto Shippuden"},
        ]
        self.assertEqual(
            HindiProvider._desidubanime_select_seasons("naruto-shippuuden", seasons),
            [seasons[1]],
        )
        embed_id = ":".join(
            base64.b64encode(value.encode()).decode().rstrip("=")
            for value in ("Abyss", "https://play.abyssplayer.com/example")
        )
        self.assertEqual(
            HindiProvider._decode_desidubanime_embed(embed_id),
            {"name": "Abyss", "url": "https://play.abyssplayer.com/example"},
        )

        ranked = HindiProvider._rank_desidubanime_servers([
            {"name": "Abyssdub", "url": "https://play.abyssplayer.com/old"},
            {"name": "CLOUD", "url": "https://cloud.desidubanime.me/external/current"},
        ])
        self.assertEqual(ranked[0]["name"], "CLOUD")

    def test_animeworld_flattens_all_seasons(self):
        async def run():
            provider = HindiProvider()

            async def seasons(_series_id):
                return [
                    {"seasonNumber": "Season 2", "seasonId": "s2"},
                    {"seasonNumber": "Season 1", "seasonId": "s1"},
                ]

            async def episodes(season_id):
                return [
                    {"episodeId": f"{season_id}-1", "episodeNumber": "1"},
                    {"episodeId": f"{season_id}-2", "episodeNumber": "2"},
                ]

            async def api_json(_path, params=None):
                return {"stream": {"streamLink": f"https://stream.example/{params['episodeId']}"}}

            provider._animeworld_seasons = seasons
            provider._animeworld_episodes = episodes
            provider._animeworld_json = api_json

            self.assertEqual(await provider.get_episode_count("animeworld:naruto"), 4)
            self.assertEqual(
                await provider.resolve_episode("animeworld:naruto", 3),
                "https://stream.example/s2-1",
            )

        asyncio.run(run())

    def test_resolver_routes_hindi_mode(self):
        async def run():
            resolver = AniCliResolver()
            resolver._hindi_provider = FakeHindiProvider()
            results = await resolver.search_hindi("Naruto")
            self.assertEqual(results[0].provider_id, "direct:naruto")
            self.assertEqual(await resolver.get_hindi_episode_count("direct:naruto"), 12)
            self.assertEqual(
                await resolver.resolve_hindi_episode("direct:naruto", 3),
                "https://stream.example/naruto-3.m3u8",
            )

        asyncio.run(run())



if __name__ == "__main__":
    unittest.main()
