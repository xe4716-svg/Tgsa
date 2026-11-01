import pytest
from bip_utils import Bip44, Bip44Changes, Bip44Coins

from app.billing.tron_watcher import derive_tron_deposit_address


def test_tron_account_xpub_derives_expected_deposit_address():
    seed = bytes(range(32))
    account = (
        Bip44.FromSeed(seed, Bip44Coins.TRON)
        .Purpose()
        .Coin()
        .Account(0)
    )
    expected = (
        account.Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(17)
        .PublicKey()
        .ToAddress()
    )
    account_xpub = account.PublicKey().ToExtended()
    assert derive_tron_deposit_address(account_xpub, 17) == expected


def test_tron_external_chain_xpub_derives_expected_deposit_address():
    seed = bytes(range(32))
    external_chain = (
        Bip44.FromSeed(seed, Bip44Coins.TRON)
        .Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
    )
    expected = external_chain.AddressIndex(3).PublicKey().ToAddress()
    chain_xpub = external_chain.PublicKey().ToExtended()
    assert derive_tron_deposit_address(chain_xpub, 3) == expected


def test_tron_derivation_rejects_private_extended_keys():
    account = (
        Bip44.FromSeed(bytes(range(32)), Bip44Coins.TRON)
        .Purpose()
        .Coin()
        .Account(0)
    )
    xprv = account.PrivateKey().ToExtended()
    with pytest.raises(ValueError):
        derive_tron_deposit_address(xprv, 1)


def test_tron_derivation_rejects_negative_index():
    account = (
        Bip44.FromSeed(bytes(range(32)), Bip44Coins.TRON)
        .Purpose()
        .Coin()
        .Account(0)
    )
    with pytest.raises(ValueError):
        derive_tron_deposit_address(account.PublicKey().ToExtended(), -1)
