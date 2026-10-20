"""The mission's check: result.json for one district or for the whole country, recomputed from the frozen data.

It verifies every number it can compute and runs every witness. It cannot verify that a margin is the
smallest possible: that is what counterexamples are for (a smaller vote change that flips a seat).
"""

import unittest

import pkwlib as k

HEADINGS = ("## Question", "## Data", "## Method", "## Results", "## Limitations")


class Result(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = k.data()
        cls.r = k.load_result()

    def check_district(self, number, entry):
        d = self.d
        witness = entry.get("witness")
        self.assertIsInstance(witness, dict, f"district {number}: a witness (votes to add or remove per list) is required")
        votes, cost = k.apply_witness(number, witness, d)
        self.assertEqual(cost, entry["margin"], f"district {number}: the witness changes {cost} votes, the margin says "
                                                f"{entry['margin']}")
        self.assertTrue(k.flips(number, witness, d), f"district {number}: the witness does not change the seat allocation")
        volume = k.district_volume(number, d)
        self.assertEqual(entry["volume"], volume, f"district {number}: volumes differ from the definitions")
        self.assertEqual(entry["robust"], {"strict": volume["strict"] < entry["margin"],
                                           "conservative": volume["conservative"] < entry["margin"]},
                         f"district {number}: robust means volume < margin")

    def test_result(self):
        kind = self.r.get("type")
        self.assertIn(kind, ("district", "national"))
        (self.district if kind == "district" else self.national)()

    def district(self):
        d, r = self.d, self.r
        number = r.get("district")
        self.assertIn(number, d["seats"], "district must be one of 1..41")
        self.assertEqual(r["votes"], d["votes"][number], "votes must be PKW's official district totals")
        self.assertEqual(r["seats"], k.district_seats(number, None, d))
        self.check_district(number, r)
        self.assertEqual(r["top_stations"], k.top_stations(number, 5, d))
        self.assertGreaterEqual(len(str(r.get("notes", ""))), 80,
                                "notes must explain the largest sources of inconsistency (at least a few sentences)")

    def national(self):
        d, r = self.d, self.r
        national = k.national_votes(d)
        self.assertEqual(r["votes"], national)
        self.assertEqual(sorted(r["eligible"]), sorted(k.national_eligible(d)))
        total = sum(national.values())
        expected = {}
        for lid, v in national.items():
            t = 8 if lid in d["coalitions"] else 5
            if lid in d["minorities"]:
                expected[lid] = None                                         # exempt: no threshold to cross
            elif v * 100 >= t * total:                                       # votes it can lose and still qualify, + 1
                expected[lid] = (100 * v - t * total) // (100 - t) + 1
            else:                                                            # votes it must gain to qualify
                expected[lid] = -(-(t * total - 100 * v) // (100 - t))
        self.assertEqual(r["threshold_margins"], expected)
        seats = {lid: 0 for lid in d["lists"]}
        for number in d["seats"]:
            for lid, n in k.district_seats(number, None, d).items():
                seats[lid] += n
        self.assertEqual(r["seats"], seats)
        self.assertEqual(seats, d["official_seats"], "the recomputation must reproduce PKW's 460 seats")
        self.assertEqual(sorted(map(int, r["districts"])), sorted(d["seats"]), "all 41 districts are required")
        for key, entry in r["districts"].items():
            with self.subTest(district=key):
                self.check_district(int(key), entry)
        for kind in ("strict", "conservative"):
            self.assertEqual(sorted(r["not_robust"][kind]),
                             sorted(int(key) for key, e in r["districts"].items() if not e["robust"][kind]))
        report = r.get("report", "")
        for heading in HEADINGS:
            self.assertIn(heading, report)
        self.assertIn("not evidence of irregularities", report.lower(),
                      "the report must say that a robustness bound is not evidence of irregularities")
