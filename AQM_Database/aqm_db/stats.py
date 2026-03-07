from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.types import StorageReport, VaultStats
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory


class StorageReporter:
    def __init__(self, vault: SecureVault, inventory: SmartInventory):
        self.vault = vault
        self.inventory = inventory

    def get_storage_usage(self) -> StorageReport:
        all_summaries = self.inventory.get_inventory()  # dict[str, InventorySummary]
        total_bytes = 0
        per_contact: dict[str, int] = {}

        for contact_id, summary in all_summaries.items():
            contact_bytes = (
                    summary.gold_count * config.COIN_SIZE_BYTES["GOLD"]
                    + summary.silver_count * config.COIN_SIZE_BYTES["SILVER"]
                    + summary.bronze_count * config.COIN_SIZE_BYTES["BRONZE"]
            )
            per_contact[contact_id] = contact_bytes
            total_bytes += contact_bytes

        budget = config.INV_MAX_STORAGE_BYTES
        utilization = (total_bytes / budget * 100) if budget > 0 else 0.0

        return StorageReport(
            total_bytes=total_bytes,
            per_contact=per_contact,
            budget_bytes=budget,
            utilization_pct=round(utilization, 2),
        )

    def get_vault_report(self) -> VaultStats:
        return self.vault.get_stats()

    def get_replenish_needs(self) -> dict[str, dict[str, int]]:
        all_summaries = self.inventory.get_inventory()
        needs: dict[str, dict[str, int]] = {}

        for contact_id, summary in all_summaries.items():
            if summary.priority == "STRANGER":
                continue

            caps = config.BUDGET_CAPS[summary.priority]
            deficit = {
                "GOLD": max(0, caps["GOLD"] - summary.gold_count),
                "SILVER": max(0, caps["SILVER"] - summary.silver_count),
                "BRONZE": max(0, caps["BRONZE"] - summary.bronze_count),
            }

            if any(v > 0 for v in deficit.values()):
                needs[contact_id] = deficit

        return needs

    def get_full_dashboard(self) -> dict:
        all_summaries = self.inventory.get_inventory()
        contacts_list = list(all_summaries.values()) if isinstance(all_summaries, dict) else []

        return {
            "vault": self.vault.get_stats(),
            "inventory_storage": self.get_storage_usage(),
            "replenish_needs": self.get_replenish_needs(),
            "contacts": contacts_list,
        }