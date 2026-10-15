"""Route content. Add a future climb by appending another challenge dict."""

from __future__ import annotations

FEATURED_SLUG = "everest"
RESERVED_SLUGS = {"join", "login", "logout", "api", "static", "favicon.ico"}
UNLOCK_KINDS = {"scene", "tidbit", "recipe"}

EVEREST = {
    "slug": "everest",
    "name": "Everest",
    "local_names": ["Sagarmatha", "Chomolungma"],
    "region": "Khumbu, Nepal",
    "route": "Nepal side, then the Southeast Ridge",
    "summit_ft": 29032,
    "summary": (
        "Start at sea level and log the elevation you gain on real outings. "
        "A camp opens when your total reaches that place's height."
    ),
    "stops": [
        {
            "slug": "lukla",
            "name": "Lukla",
            "elevation_ft": 9383,
            "place": "village",
            "summary": "The airstrip where the walk begins.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "A runway aimed at the hillside",
                    "body": (
                        "Lukla sits on a shelf above the Dudh Kosi. The runway "
                        "is only about 1,700 feet long, and it slopes uphill on "
                        "purpose: landings run up the slope, takeoffs run down. "
                        "The field is Tenzing-Hillary Airport. It opened in 1964 "
                        "and is still the usual way into the Khumbu. Flights wait "
                        "on the weather. Mornings are the bet, before cloud fills "
                        "the valley."
                    ),
                },
                {
                    "kind": "recipe",
                    "title": "Butter tea",
                    "makes": "2 small cups",
                    "ingredients": [
                        "2 cups water",
                        "1 tablespoon loose black tea, or 2 tea bags",
                        "1/4 teaspoon salt",
                        "1 tablespoon unsalted butter",
                    ],
                    "steps": [
                        "Boil the water, add the tea, and steep it for about 4 minutes.",
                        "Strain it. Add the salt and the butter.",
                        "Blend or whisk hard until the tea looks opaque and the butter is mixed in, not floating as a slick.",
                        "Drink it hot.",
                    ],
                    "aside": (
                        "In a Sherpa or Tibetan kitchen this is po cha, churned "
                        "with yak butter. A blender and ordinary butter is the "
                        "home version. It should taste savory, not sweet."
                    ),
                },
            ],
        },
        {
            "slug": "namche",
            "name": "Namche Bazaar",
            "elevation_ft": 11286,
            "place": "village",
            "summary": "The Sherpa town in the bowl.",
            "unlocks": [
                {
                    "kind": "tidbit",
                    "title": "Market town, and a forced slow-down",
                    "body": (
                        "Namche Bazaar is the main town of the Khumbu, built in "
                        "a horseshoe of hillside. The Saturday market still pulls "
                        "goods up from lower valleys. Most trekking groups stay "
                        "two nights. The extra day is the point: walk higher in "
                        "the afternoon, then sleep back down in town. On this "
                        "trail that is called climbing high and sleeping low."
                    ),
                },
                {
                    "kind": "recipe",
                    "title": "Dal bhat",
                    "makes": "4 plates",
                    "ingredients": [
                        "1 cup red lentils, rinsed",
                        "3 cups water",
                        "1/2 teaspoon turmeric",
                        "Salt",
                        "1 tablespoon oil or ghee",
                        "1 small onion, chopped",
                        "2 garlic cloves and a thumb of ginger, chopped",
                        "1 teaspoon ground cumin",
                        "A pinch of chili",
                        "Rice for four",
                        "One vegetable you have: potato, cabbage, or spinach",
                    ],
                    "steps": [
                        "Simmer the lentils with the turmeric until they slump and you can crush them with a spoon. Salt them.",
                        "Warm the oil. Cook the onion, then the garlic and ginger, then the cumin and chili.",
                        "Stir that into the lentils. Add a splash of water if the dal is stiff.",
                        "Cook the rice. Cook the vegetable simply, with salt and a little of the same spices.",
                        "Serve rice, a ladle of dal, and the vegetable on one plate.",
                    ],
                    "aside": (
                        "On the trek, dal bhat shows up almost every day. The "
                        "pot comes back for another helping until you wave it off."
                    ),
                },
            ],
        },
        {
            "slug": "tengboche",
            "name": "Tengboche",
            "elevation_ft": 12664,
            "place": "village",
            "summary": "A monastery terrace, with Ama Dablam in the window.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "Ama Dablam over the ridge",
                    "body": (
                        "The walk into Tengboche opens onto a monastery terrace. "
                        "Ama Dablam stands across the valley, sharp and ridged. "
                        "Everest is still far back, behind Lhotse and Nuptse. "
                        "Tengboche Monastery is a Sherpa Buddhist house. A fire "
                        "in 1989 badly damaged it, and it was rebuilt. Shoes come "
                        "off inside, and voices drop."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "Stones with a mantra",
                    "body": (
                        "Mani stones along this trail are carved with prayers, "
                        "often om mani padme hum. They are religious objects. "
                        "The custom is to pass a wall with the stones on your "
                        "right, which keeps you walking clockwise, and to leave "
                        "the stones where they are."
                    ),
                },
            ],
        },
        {
            "slug": "dingboche",
            "name": "Dingboche",
            "elevation_ft": 14469,
            "place": "village",
            "summary": "Stone-walled fields, and a rest day.",
            "unlocks": [
                {
                    "kind": "tidbit",
                    "title": "The potato that moved uphill",
                    "body": (
                        "Dingboche is one of the last villages. The fields are "
                        "walled in stone so wind and livestock stay out of the "
                        "potatoes. The potato reached the Khumbu in the nineteenth "
                        "century and became a crop that could live this high. "
                        "Groups usually take a rest day and walk toward the "
                        "Nangkartshang ridge, then come back down to sleep. A "
                        "headache or a sick stomach is a reason to stop going up. "
                        "If it does not ease, the trail's rule is to walk down."
                    ),
                },
                {
                    "kind": "recipe",
                    "title": "Potato cakes",
                    "makes": "about 8 small cakes",
                    "ingredients": [
                        "3 medium potatoes, boiled and mashed",
                        "1/2 cup flour",
                        "1/2 teaspoon salt",
                        "2 green onions, chopped, if you have them",
                        "A spoon of oil for the pan",
                    ],
                    "steps": [
                        "Mix the potatoes, flour, salt, and onion into a soft dough. Add a teaspoon of water only if it will not hold.",
                        "Pat small cakes about the width of your palm.",
                        "Cook them in a lightly oiled pan over medium heat until both sides are spotted brown.",
                    ],
                    "aside": (
                        "Sherpa kitchens make a potato bread often called riki kur. "
                        "Wheat flour is the easy stand-in when there is no buckwheat "
                        "or millet in the cupboard."
                    ),
                },
            ],
        },
        {
            "slug": "lobuche",
            "name": "Lobuche",
            "elevation_ft": 16207,
            "place": "village",
            "summary": "A short string of lodges on the moraine.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "The valley narrows",
                    "body": (
                        "Past the last potato fields the valley turns to rock "
                        "and glacier rubble. Lobuche is a few lodges tucked "
                        "against the moraine of the Khumbu Glacier. The air is "
                        "thin enough that a walk between buildings counts. "
                        "Evenings are soup and an early night."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "The ridge of chortens",
                    "body": (
                        "The path up from Thukla crosses a windy shelf of "
                        "memorial chortens. They remember climbers and Sherpa "
                        "guides who died on Everest. Walk through quietly, and "
                        "leave the stacks of stones as you found them."
                    ),
                },
            ],
        },
        {
            "slug": "gorak-shep",
            "name": "Gorak Shep",
            "elevation_ft": 16942,
            "place": "village",
            "summary": "The last lodges, on a dry lakebed.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "A settlement on sand and gravel",
                    "body": (
                        "Gorak Shep is barely a village. It is a handful of "
                        "lodges on the flat of an old lake, with the Khumbu "
                        "Glacier beside it. This is the usual bed before a dark "
                        "start for Kala Patthar, and the turnoff for base camp."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "The kettle takes longer",
                    "body": (
                        "Water here boils well below 100°C, around 83°C. Tea "
                        "still gets made, and noodles still get cooked, but the "
                        "pot never rolls the way it does at home, and a hot drink "
                        "cools fast. Dinner just takes a while. That is the "
                        "physics of the height, not a failing of the kitchen."
                    ),
                },
            ],
        },
        {
            "slug": "base-camp",
            "name": "Everest Base Camp",
            "elevation_ft": 17598,
            "place": "camp",
            "summary": "A tent city with a short season.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "Base camp, with the summit out of sight",
                    "body": (
                        "Nepal-side base camp is a seasonal city of tents on "
                        "the moving ice of the Khumbu Glacier. It fills for the "
                        "spring climbing season and packs out again. The summit "
                        "is mostly hidden from here by the West Shoulder and by "
                        "Nuptse. Just beyond camp, the Khumbu Icefall is a broken "
                        "glacier. Climbers thread it in the coldest hours, before "
                        "the sun loosens the seracs."
                    ),
                },
                {
                    "kind": "recipe",
                    "title": "Garlic soup",
                    "makes": "2 bowls",
                    "ingredients": [
                        "1 head of garlic, cloves peeled and sliced",
                        "1 tablespoon butter or oil",
                        "4 cups vegetable or chicken broth",
                        "Salt and black pepper",
                        "An egg, if you want it",
                        "Bread, or a handful of noodles",
                    ],
                    "steps": [
                        "Warm the butter. Cook the garlic slowly until it smells sweet and turns pale gold. Do not let it go dark.",
                        "Add the broth and simmer for about 15 minutes.",
                        "Salt and pepper it. For the lodge version, whisk an egg in a cup and stir it into the simmering soup.",
                        "Eat it with bread, or add the noodles for the last few minutes.",
                    ],
                    "aside": (
                        "Khumbu teahouses serve this all the time. It is comfort "
                        "when people feel the height. It is not medicine, and it "
                        "does not replace a rest day or a walk back down."
                    ),
                },
            ],
        },
        {
            "slug": "kala-patthar",
            "name": "Kala Patthar",
            "elevation_ft": 18520,
            "place": "viewpoint",
            "summary": "The lookout above Gorak Shep.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "The view people actually came for",
                    "body": (
                        "Kala Patthar is a dark ridge above Gorak Shep, and it "
                        "is the classic place to see the summit. From base camp "
                        "the top is mostly hidden. From here the Southeast Ridge, "
                        "the Western Cwm, and the face of Nuptse line up together. "
                        "A lot of people leave Gorak Shep in the dark and climb "
                        "by headlamp, so they are up there when the summit takes "
                        "the first light."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "A lookout, not a step on the climb",
                    "body": (
                        "This spot sits off the way to the icefall. Trekkers go "
                        "up and come back to Gorak Shep the same morning. The "
                        "climbing route does not pass the viewpoint on the way "
                        "to Camp I. It is in this list because of its height, "
                        "and because this is the picture most people mean when "
                        "they say they saw Everest."
                    ),
                },
            ],
        },
        {
            "slug": "camp-1",
            "name": "Camp I",
            "elevation_ft": 19898,
            "place": "camp",
            "summary": "Above the icefall, at the door of the Western Cwm.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "Out of the icefall",
                    "body": (
                        "Camp I sits where the Khumbu Icefall opens into the "
                        "Western Cwm. Reaching it is the first real climbing on "
                        "the South Col route: ladders over crevasses, fixed rope, "
                        "and a start in the dark. The camp is a pause on the way "
                        "up, not a place to linger for the view."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "Why the start is so early",
                    "body": (
                        "The icefall moves. Seracs fall, and the bridges over "
                        "crevasses slump once the sun is on them. Teams leave "
                        "base camp at night or before dawn so they are through "
                        "the worst of it while the ice is colder. The ladders "
                        "get reset through the season, because yesterday's bridge "
                        "is not a promise."
                    ),
                },
            ],
        },
        {
            "slug": "camp-2",
            "name": "Camp II",
            "elevation_ft": 20997,
            "place": "camp",
            "summary": "Advanced base, in a valley of ice.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "The Western Cwm",
                    "body": (
                        "Camp II, often called advanced base, sits in the "
                        "Western Cwm: a long trough of ice between Everest and "
                        "the wall of Lhotse and Nuptse. Sun bounces off the "
                        "walls. On a still day it can feel strangely warm for "
                        "a place already higher than Denali. The night goes "
                        "the other way."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "A Welsh word from 1921",
                    "body": (
                        "George Mallory used the Welsh word cwm, a valley, for "
                        "this place on the 1921 British reconnaissance. The name "
                        "stuck. Climbers sometimes call it the Valley of Silence. "
                        "In a busy season it is a working camp: tents, supplies, "
                        "and rest before the Lhotse Face."
                    ),
                },
            ],
        },
        {
            "slug": "camp-3",
            "name": "Camp III",
            "elevation_ft": 23622,
            "place": "camp",
            "summary": "Platforms cut into the Lhotse Face.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "The steep ice above the cwm",
                    "body": (
                        "Camp III is cut into the Lhotse Face, the wall of ice "
                        "that climbs out of the Western Cwm toward the South Col. "
                        "Tents sit on scraped platforms, often with the slope "
                        "dropping away at the door. Movement here is slow and "
                        "usually clipped into fixed ropes."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "Bottled oxygen, depending on the team",
                    "body": (
                        "There is no single rule for bottled oxygen. Many "
                        "guided teams start using it on the Lhotse Face or at "
                        "the South Col. Climbing without it all the way is a "
                        "different, and much rarer, project. Either way, the "
                        "route from here on is a high climb."
                    ),
                },
            ],
        },
        {
            "slug": "south-col",
            "name": "South Col",
            "elevation_ft": 26083,
            "place": "camp",
            "summary": "The windy saddle between Everest and Lhotse.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "A high, scoured pass",
                    "body": (
                        "The South Col is the saddle between Everest and Lhotse, "
                        "and Camp IV sits on it. There is almost no shelter. Wind "
                        "rakes it, and outside the short climbing seasons the jet "
                        "stream can drop onto this part of the mountain. Most "
                        "summit bids leave from here at night and hope to be on "
                        "top near dawn, with enough daylight left to get down."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "29 May 1953",
                    "body": (
                        "Edmund Hillary and Tenzing Norgay reached the summit "
                        "on 29 May 1953, by this Southeast Ridge, with John Hunt's "
                        "British expedition. They were the first people confirmed "
                        "to have climbed Everest. The expedition pushed its high "
                        "camps up the same line this route still follows: the "
                        "icefall, the cwm, the Lhotse Face, and the col."
                    ),
                },
            ],
        },
        {
            "slug": "summit",
            "name": "Summit",
            "elevation_ft": 29032,
            "place": "summit",
            "summary": "The crest of the Southeast Ridge.",
            "unlocks": [
                {
                    "kind": "scene",
                    "title": "A small crest, and a long way down",
                    "body": (
                        "The summit is a short crest of snow, not a wide plateau. "
                        "People stay briefly, then turn around. The harder half "
                        "is the descent: the same ridge, the col, and the icefall, "
                        "on tired legs. The rock step below the top, the Hillary "
                        "Step, was reshaped by the 2015 earthquake. Climbers still "
                        "move through that section on fixed rope."
                    ),
                },
                {
                    "kind": "tidbit",
                    "title": "29,032 feet",
                    "body": (
                        "In December 2020, Nepal and China jointly announced a "
                        "height of 8,848.86 meters, which rounds to 29,032 feet. This "
                        "climb uses that figure and starts at sea level. Everest "
                        "is Sagarmatha in Nepali, and Chomolungma in Sherpa and "
                        "Tibetan, usually translated as Goddess Mother of the World."
                    ),
                },
            ],
        },
    ],
}

CHALLENGES = [EVEREST]
PLACE_LABELS = {
    "village": "Village",
    "camp": "Camp",
    "viewpoint": "Viewpoint",
    "summit": "Summit",
}


def _validate(challenge: dict) -> None:
    slug = challenge["slug"]
    if slug in RESERVED_SLUGS:
        raise RuntimeError(f"{slug} is a reserved name")
    elevations = [stop["elevation_ft"] for stop in challenge["stops"]]
    if elevations != sorted(set(elevations)):
        raise RuntimeError(f"{slug} stops must climb, with no repeated heights")
    if elevations[-1] != challenge["summit_ft"]:
        raise RuntimeError(f"{slug} summit stop must match summit_ft")
    slugs = [stop["slug"] for stop in challenge["stops"]]
    if len(slugs) != len(set(slugs)):
        raise RuntimeError(f"{slug} has a repeated stop slug")
    for stop in challenge["stops"]:
        if stop["place"] not in PLACE_LABELS:
            raise RuntimeError(f"{stop['slug']} has an unknown place type")
        if not stop["unlocks"]:
            raise RuntimeError(f"{stop['slug']} has no unlocks")
        for item in stop["unlocks"]:
            if item["kind"] not in UNLOCK_KINDS:
                raise RuntimeError(f"{stop['slug']} has an unknown unlock")
            if item["kind"] == "recipe" and not (item.get("ingredients") and item.get("steps")):
                raise RuntimeError(f"{stop['slug']} recipe is incomplete")


for _challenge in CHALLENGES:
    _validate(_challenge)


def all_challenges() -> list[dict]:
    return list(CHALLENGES)


def get_challenge(slug: str) -> dict:
    for challenge in CHALLENGES:
        if challenge["slug"] == slug:
            return challenge
    raise KeyError(slug)
