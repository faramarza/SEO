from src.data_sources.magento_client import MagentoClient


class FakeMC(MagentoClient):
    """Fake Magento: a description per scope; 'store' falls back to global when unset."""
    def __init__(self, glob, store=None):
        super().__init__(base_url="http://x", token="t", store_code="alphabettrains")
        self.vals = {"all": glob, "alphabettrains": store}
        self.puts = []

    def _req(self, method, path, payload=None, scope="all"):
        if method == "GET":
            v = self.vals.get(scope)
            if v is None:
                v = self.vals["all"]
            return {"custom_attributes": [{"attribute_code": "description", "value": v}]}
        self.puts.append(scope)
        new = payload[list(payload)[0]]["custom_attributes"][0]["value"]
        self.vals[scope] = new
        return {}


def test_no_store_copy_writes_global_only():
    mc = FakeMC("<p>Hi</p>")
    assert mc.write_description_scoped("category", 5, "<p>Hi link</p>",
                                       {"category": {"custom_attributes": [{"attribute_code": "description", "value": "<p>Hi link</p>"}]}}) == ["all"]
    assert mc.puts == ["all"] and mc.vals["alphabettrains"] is None


def test_store_copy_writes_store_only():
    mc = FakeMC("<p>Global</p>", store="<p>Store wording</p>")
    sc = mc.scoped_description("category", 5)
    assert sc["override"] and sc["store"] == "<p>Store wording</p>"
    mc.write_description_scoped("category", 5, "<p>Store wording link</p>",
                                {"category": {"custom_attributes": [{"attribute_code": "description", "value": "<p>Store wording link</p>"}]}})
    assert mc.puts == ["alphabettrains"] and mc.vals["all"] == "<p>Global</p>"
