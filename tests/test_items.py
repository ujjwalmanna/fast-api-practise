"""Item CRUD, pagination and ownership isolation."""

from __future__ import annotations

from httpx import AsyncClient

ITEMS = "/api/v1/items"


async def _create(
    client: AsyncClient, headers: dict[str, str], name: str = "Widget", price: str = "9.99"
) -> dict[str, object]:
    response = await client.post(
        ITEMS, headers=headers, json={"name": name, "price": price, "description": "d"}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- create ---------------------------------------------------------------


async def test_create_item_assigns_ownership_from_the_token(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    me = (await client.get("/api/v1/auth/me", headers=user_headers)).json()
    item = await _create(client, user_headers)

    assert item["owner_id"] == me["id"]
    assert item["name"] == "Widget"
    assert item["price"] == "9.99"


async def test_owner_id_in_the_body_is_ignored(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    """Ownership must come from the verified token, never from user input."""
    response = await client.post(
        ITEMS,
        headers=user_headers,
        json={"name": "Spoofed", "price": "1.00", "owner_id": "somebody-else"},
    )
    assert response.status_code == 201

    me = (await client.get("/api/v1/auth/me", headers=user_headers)).json()
    assert response.json()["owner_id"] == me["id"]


async def test_create_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(ITEMS, json={"name": "X", "price": "1.00"})
    assert response.status_code == 401


async def test_create_rejects_a_negative_price(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    response = await client.post(ITEMS, headers=user_headers, json={"name": "X", "price": "-1.00"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "price"


async def test_create_rejects_a_blank_name(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    """`strip_whitespace` plus `min_length=1` must reject whitespace-only input."""
    response = await client.post(ITEMS, headers=user_headers, json={"name": "   ", "price": "1.00"})
    assert response.status_code == 422


async def test_create_rejects_excess_decimal_places(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    """Rejected at the edge rather than silently rounded by the database."""
    response = await client.post(ITEMS, headers=user_headers, json={"name": "X", "price": "1.239"})
    assert response.status_code == 422


# --- read -----------------------------------------------------------------


async def test_get_item(client: AsyncClient, user_headers: dict[str, str]) -> None:
    created = await _create(client, user_headers)
    response = await client.get(f"{ITEMS}/{created['id']}", headers=user_headers)
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_missing_item_is_404_with_the_problem_envelope(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    missing = "0" * 36
    response = await client.get(f"{ITEMS}/{missing}", headers=user_headers)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# --- pagination -----------------------------------------------------------


async def test_list_is_paginated_and_reports_the_true_total(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    for index in range(5):
        await _create(client, user_headers, name=f"Item {index}")

    first = await client.get(ITEMS, headers=user_headers, params={"limit": 2, "offset": 0})
    assert first.status_code == 200

    body = first.json()
    assert len(body["items"]) == 2
    assert body["total"] == 5  # total ignores pagination
    assert body["limit"] == 2
    assert body["offset"] == 0

    second = await client.get(ITEMS, headers=user_headers, params={"limit": 2, "offset": 2})
    assert len(second.json()["items"]) == 2

    # Pages must not overlap - this is what the id tie-break in the repository
    # ordering guarantees.
    ids = {item["id"] for item in body["items"]} | {item["id"] for item in second.json()["items"]}
    assert len(ids) == 4


async def test_limit_is_capped(client: AsyncClient, user_headers: dict[str, str]) -> None:
    """An uncapped page size is a one-request denial of service."""
    response = await client.get(ITEMS, headers=user_headers, params={"limit": 10_000})
    assert response.status_code == 422


async def test_negative_offset_is_rejected(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    response = await client.get(ITEMS, headers=user_headers, params={"offset": -1})
    assert response.status_code == 422


async def test_search_filters_by_name(client: AsyncClient, user_headers: dict[str, str]) -> None:
    await _create(client, user_headers, name="Blue Widget")
    await _create(client, user_headers, name="Red Gadget")

    response = await client.get(ITEMS, headers=user_headers, params={"search": "widget"})
    assert response.status_code == 200

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Blue Widget"


async def test_search_escapes_sql_wildcards(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    """A literal `%` must be searched for, not treated as "match everything"."""
    await _create(client, user_headers, name="50% off")
    await _create(client, user_headers, name="Nothing special")

    response = await client.get(ITEMS, headers=user_headers, params={"search": "%"})
    assert response.status_code == 200
    assert response.json()["total"] == 1


# --- update / delete ------------------------------------------------------


async def test_patch_updates_only_the_supplied_fields(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    created = await _create(client, user_headers, name="Before", price="5.00")

    response = await client.patch(
        f"{ITEMS}/{created['id']}", headers=user_headers, json={"name": "After"}
    )
    assert response.status_code == 200

    body = response.json()
    assert body["name"] == "After"
    # Untouched fields must survive - the `exclude_unset` behaviour.
    assert body["price"] == "5.00"
    assert body["description"] == "d"


async def test_delete_removes_the_item(client: AsyncClient, user_headers: dict[str, str]) -> None:
    created = await _create(client, user_headers)

    assert (
        await client.delete(f"{ITEMS}/{created['id']}", headers=user_headers)
    ).status_code == 204
    assert (await client.get(f"{ITEMS}/{created['id']}", headers=user_headers)).status_code == 404


# --- ownership isolation --------------------------------------------------


async def test_a_user_cannot_read_another_users_item(
    client: AsyncClient,
    user_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    """404, not 403: confirming the row exists would enable id enumeration."""
    mine = await _create(client, user_headers, name="Private")

    response = await client.get(f"{ITEMS}/{mine['id']}", headers=other_user_headers)
    assert response.status_code == 404


async def test_a_user_cannot_modify_or_delete_another_users_item(
    client: AsyncClient,
    user_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    mine = await _create(client, user_headers, name="Private")

    assert (
        await client.patch(
            f"{ITEMS}/{mine['id']}", headers=other_user_headers, json={"name": "Hijacked"}
        )
    ).status_code == 404
    assert (
        await client.delete(f"{ITEMS}/{mine['id']}", headers=other_user_headers)
    ).status_code == 404

    # And it is genuinely untouched.
    assert (await client.get(f"{ITEMS}/{mine['id']}", headers=user_headers)).json()[
        "name"
    ] == "Private"


async def test_list_is_scoped_to_the_caller(
    client: AsyncClient,
    user_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    await _create(client, user_headers, name="Mine")
    await _create(client, other_user_headers, name="Theirs")

    mine = await client.get(ITEMS, headers=user_headers)
    assert mine.json()["total"] == 1
    assert mine.json()["items"][0]["name"] == "Mine"


async def test_an_admin_sees_every_item(
    client: AsyncClient,
    user_headers: dict[str, str],
    other_user_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    await _create(client, user_headers, name="Mine")
    await _create(client, other_user_headers, name="Theirs")

    response = await client.get(ITEMS, headers=admin_headers)
    assert response.json()["total"] == 2


async def test_mine_only_narrows_an_admins_view(
    client: AsyncClient,
    user_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    await _create(client, user_headers, name="Mine")
    await _create(client, admin_headers, name="Admin's own")

    response = await client.get(ITEMS, headers=admin_headers, params={"mine_only": True})
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["name"] == "Admin's own"


async def test_deleting_a_user_cascades_to_their_items(
    client: AsyncClient,
    user_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """Proves `PRAGMA foreign_keys=ON` is actually in effect.

    Without that pragma SQLite ignores `ON DELETE CASCADE` entirely and this
    test fails with orphaned rows - which is exactly the silent bug it guards.
    """
    me = (await client.get("/api/v1/auth/me", headers=user_headers)).json()
    await _create(client, user_headers, name="Doomed")

    assert (
        await client.delete(f"/api/v1/users/{me['id']}", headers=admin_headers)
    ).status_code == 204

    # Admin sees all items; the cascade must have removed the orphan.
    assert (await client.get(ITEMS, headers=admin_headers)).json()["total"] == 0
