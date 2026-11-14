from src.data_sources.magento_client import is_sellable


def prod(**kw):
    p = {"status": 1, "name": "Guidecraft Peekaboo Lock Boxes", "custom_attributes": [
        {"attribute_code": "brand_google", "value": "Guide Craft"}],
         "extension_attributes": {"stock_item": {"is_in_stock": True}}}
    p.update(kw)
    return p


def test_sellable_rules():
    closed = {"tag toys"}
    assert is_sellable(prod(), closed)
    assert not is_sellable(prod(status=2), closed)
    assert not is_sellable(prod(extension_attributes={"stock_item": {"is_in_stock": False}}), closed)
    assert not is_sellable(prod(name="Montessori Lock Box discontinued"), closed)
    assert not is_sellable(prod(custom_attributes=[{"attribute_code": "brand_google", "value": "Tag Toys"}]), closed)
    assert is_sellable(prod(extension_attributes={}), closed)   # stock unknown: keep


def test_config_marks_tag_closed():
    from src.data_sources.magento_client import discontinued_brands
    assert "tag toys" in discontinued_brands()
