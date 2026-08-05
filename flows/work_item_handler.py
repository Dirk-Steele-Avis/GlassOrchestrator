"""
Work Item Handler Pattern for Different Work Item Types.

This module provides a flexible framework for handling different work item types
(Glass, PM, Brake, etc.) with their specific workflows while sharing common logic.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any
from utils.logger import log
from core.complaint_types import GlassDamageType

@dataclass
class WorkItemConfig:
    """Configuration data for creating work items from CSV data."""
    mva: str
    damage_type: Optional[str] = None
    location: Optional[str] = None
    
    def __post_init__(self):
        """Validate and normalize config data."""
        self.mva = self.mva.strip()
        if self.damage_type:
            self.damage_type = self.damage_type.strip().upper()
        if self.location:
            self.location = self.location.strip().upper()

class WorkItemHandler(ABC):
    """Abstract base class for work item creation handlers."""
    
    def __init__(self, driver):
        """Initialize handler with WebDriver instance."""
        self.driver = driver
        self._current_mva = None
    
    @abstractmethod
    def get_work_item_type(self) -> str:
        """Return the work item type identifier (e.g., 'GLASS', 'PM')."""
        pass
    
    @abstractmethod
    def should_handle_existing_complaint(self, complaint_text: str) -> bool:
        """Determine if an existing complaint matches this work item type."""
        pass
    
    @abstractmethod
    def detect_complaints(self, driver) -> list:
        """Return complaint tile elements relevant to this work item type."""
        pass

    @abstractmethod
    def create_new_complaint(self, config: WorkItemConfig) -> Dict[str, Any]:
        """Create a new complaint for this work item type."""
        pass
    
    @abstractmethod
    def handle_existing_complaint(self, config: WorkItemConfig, complaint_element) -> Dict[str, Any]:
        """Handle workflow when existing complaint is found."""
        pass
    
    # ----------------------------------------------------------------------------
    # AUTHOR:       Dirk Steele <dirk.avis@gmail.com>
    # DATE:         2026-01-12
    # DESCRIPTION:  Main entry point for creating work items using this handler.
    #               Orchestrates the common workflow: click Add Work Item, check for
    #               existing complaints, associate or create complaint, and complete
    #               work item-specific steps.
    # VERSION:      1.0.0
    # NOTES:        Used by all handler subclasses.
    # ----------------------------------------------------------------------------
    def create_work_item(self, config: WorkItemConfig) -> Dict[str, Any]:
        self._current_mva = config.mva
        log.info(f"[WORKITEM] {config.mva} - Creating {self.get_work_item_type()} work item")
        # Step 1: Click Add Work Item button (common for all types)
        if not self._click_add_work_item_button(config):
            return {"status": "failed", "reason": "add_btn", "mva": config.mva}
        # Step 2: Handle complaint logic (type-specific)
        return self._handle_complaint_flow(config)
    
    # ----------------------------------------------------------------------------
    # AUTHOR:       Dirk Steele <dirk.avis@gmail.com>
    # DATE:         2026-01-12
    # DESCRIPTION:  Click the Add Work Item button. Common implementation for all handlers.
    # VERSION:      1.0.0
    # NOTES:        Waits for button, logs result, and handles exceptions.
    # ----------------------------------------------------------------------------
    def _click_add_work_item_button(self, config: WorkItemConfig) -> bool:
        from utils.ui_helpers import click_element
        from selenium.webdriver.common.by import By
        import time
        try:
            time.sleep(5)  # wait for button to appear
            if not click_element(self.driver, 
                               (By.XPATH, "//button[normalize-space()='Add Work Item']"), 
                               timeout=30, 
                               desc="Add Work Item button"):
                log.warning(f"[WORKITEM][WARN] {config.mva} - add_btn not found")
                return False
            log.info(f"[WORKITEM] {config.mva} - Add Work Item clicked")
            time.sleep(5)
            return True
        except Exception as e:
            log.warning(f"[WORKITEM][WARN] {config.mva} - add_btn failed -> {e}")
            return False
    
    # ----------------------------------------------------------------------------
    # AUTHOR:       Dirk Steele <dirk.avis@gmail.com>
    # DATE:         2026-01-12
    # DESCRIPTION:  Handle the complaint association/creation flow. Checks for existing
    #               complaints, delegates to handler or creates new complaint as needed.
    # VERSION:      1.0.0
    # NOTES:        Used by all handler subclasses.
    # ----------------------------------------------------------------------------
    def _handle_complaint_flow(self, config: WorkItemConfig) -> Dict[str, Any]:
        try:
            # Check for existing complaints that match this work item type
            existing_complaints = self.detect_complaints(self.driver)
            for complaint in existing_complaints:
                if self.should_handle_existing_complaint(complaint.text):
                    log.info(f"[{self.get_work_item_type()}] {config.mva} - Found matching existing complaint")
                    return self.handle_existing_complaint(config, complaint)
            # No matching complaint found, create new one
            log.info(f"[{self.get_work_item_type()}] {config.mva} - No matching complaint found, creating new one")
            return self.create_new_complaint(config)
        except Exception as e:
            log.warning(f"[WORKITEM][WARN] {config.mva} - complaint handling failed -> {e}")
            return {"status": "failed", "reason": "complaint_handling", "mva": config.mva}

class GlassWorkItemHandler(WorkItemHandler):
    """Handler for Glass damage work items."""
    
    def map_damage_type_to_ui(self, damage_type: str, location: str) -> str:
        """Map damage type and location to appropriate GlassDamageType enum value."""
        dt = (damage_type or "REPLACEMENT").strip().upper()
        loc = (location or "UNKNOWN").strip().upper()
        
        # Determine the appropriate glass damage type based on damage type and location
        if dt in ("REPAIR", "CHIP"):
            # Repair/chip operations
            if loc in ("FRONT", "WINDSHIELD", "CHIP"):
                return GlassDamageType.WINDSHIELD_CHIP.value
            else:
                # All non-windshield repairs map to side/rear damage
                return GlassDamageType.SIDE_REAR_WINDOW_DAMAGE.value
        else:
            # Replacement operations
            if loc in ("FRONT", "WINDSHIELD"):
                return GlassDamageType.WINDSHIELD_CRACK.value
            elif loc in ("SIDE", "REAR", "TOP", "BACK"):
                return GlassDamageType.SIDE_REAR_WINDOW_DAMAGE.value
            else:
                # Unknown location defaults to unknown damage type
                return GlassDamageType.UNKNOWN.value
    
    def get_work_item_type(self) -> str:
        """Return the work item type identifier."""
        return "GLASS"
    
    def should_handle_existing_complaint(self, complaint_text: str) -> bool:
        """
        Determine if existing complaint is open and glass-related.
        Only open complaints should be reused.
        """
        glass_keywords = ["glass", "windshield", "crack", "chip", "window"]
        complaint_lower = (complaint_text or "").lower()
        lines = [line.strip() for line in complaint_lower.splitlines() if line.strip()]
        is_open = bool(lines) and lines[0] == "open"
        return is_open and any(keyword in complaint_lower for keyword in glass_keywords)
    
    # ----------------------------------------------------------------------------
    # AUTHOR:       Dirk Steele <dirk.avis@gmail.com>
    # DATE:         2026-04-11
    # DESCRIPTION:  Detect existing glass complaint tiles for this MVA.
    #               Delegates to detect_glass_complaints() from complaints_flows.
    #               Uses _current_mva set at the start of create_work_item().
    # VERSION:      1.0.0
    # NOTES:        Called by _handle_complaint_flow() in the base class.
    # ----------------------------------------------------------------------------
    def detect_complaints(self, driver) -> list:
        """Return glass complaint tile elements relevant to this work item type."""
        from flows.complaints_flows import detect_glass_complaints
        return detect_glass_complaints(driver, mva=self._current_mva)

    # ----------------------------------------------------------------------------
    # AUTHOR:       Dirk Steele <dirk.avis@gmail.com>
    # DATE:         2026-01-12
    # DESCRIPTION:  Create new glass complaint. Handles UI flow for glass complaint
    #               creation, including damage type and location selection.
    # VERSION:      1.0.0
    # NOTES:        Uses damage type/location mapping for complaint creation.
    # ----------------------------------------------------------------------------
    def create_new_complaint(self, config: WorkItemConfig) -> Dict[str, Any]:
        from flows.complaints_flows import create_new_complaint
        # Map CSV damage type/location to UI button text
        damage_type_ui = self.map_damage_type_to_ui(config.damage_type, config.location)
        log.info(f"[GLASS] {config.mva} - Selecting UI damage type: {damage_type_ui}")
        result = create_new_complaint(self.driver, config.mva, complaint_type=damage_type_ui)
        if result.get("status") != "created":
            return result
        log.info(f"[GLASS] {config.mva} - New glass complaint created, continuing workflow")
        # Step 7: Mileage -> Next
        from flows.mileage_flows import complete_mileage_dialog
        res = complete_mileage_dialog(self.driver, config.mva)
        if res.get("status") != "ok":
            return {"status": "failed", "reason": "mileage", "mva": config.mva}
        # Step 8: OpCode -> Glass Repair/Replace (with fallback)
        from flows.opcode_flows import select_opcode
        from config.config_loader import get_config
        opcode = get_config("glass_opcode_primary", "Glass Repair/Replace")
        res = select_opcode(self.driver, config.mva, code_text=opcode)
        if res.get("status") != "ok":
            res = select_opcode(self.driver, config.mva, code_text=get_config("glass_opcode_fallback", "Glass"))
        if res.get("status") != "ok":
            return {"status": "failed", "reason": "opcode", "mva": config.mva}
        # Steps 9-10: Create Work Item -> Done
        from flows.finalize_flow import finalize_workitem
        return finalize_workitem(self.driver, config.mva)
    
    # ----------------------------------------------------------------------------
    # AUTHOR:       Dirk Steele <dirk.avis@gmail.com>
    # DATE:         2026-01-12
    # DESCRIPTION:  Handle existing glass complaint scenarios. Associates complaint,
    #               manages UI flow for repair/replacement, mileage, and OpCode.
    # VERSION:      1.0.0
    # NOTES:        Existing complaint association includes mileage and OpCode flow.
    # ----------------------------------------------------------------------------
    def handle_existing_complaint(self, config: WorkItemConfig, complaint_element) -> Dict[str, Any]:
        from flows.complaints_flows import associate_existing_complaint
        from flows.finalize_flow import finalize_workitem
        try:
            # Associate the existing complaint
            result = associate_existing_complaint(self.driver, config.mva)
            if result.get("status") == "associated":
                log.info(f"[GLASS] {config.mva} - Existing glass complaint associated")
                if (config.damage_type or "").upper() == "REPLACEMENT":
                    log.info(f"[GLASS] {config.mva} - Replacement flow requested")

                finalize_result = finalize_workitem(self.driver, config.mva)
                if finalize_result.get("status") in ("closed", "ok"):
                    return {"status": "created", "mva": config.mva}
                return finalize_result
            else:
                log.warning(f"[GLASS] {config.mva} - Failed to associate existing complaint")
                return result
        except Exception as e:
            log.error(f"[GLASS] {config.mva} - Error handling existing complaint: {e}")
            return {"status": "failed", "reason": "existing_complaint_error", "mva": config.mva}

# Handler factory for future expansion
def create_work_item_handler(work_item_type: str, driver) -> WorkItemHandler:
    """
    Factory function to create appropriate work item handler.
    
    Args:
        work_item_type: Type identifier (inferred from script context)
        driver: WebDriver instance
    Returns:
        Appropriate handler instance
    """
    if work_item_type.upper() == "GLASS":
        return GlassWorkItemHandler(driver)
    # To add a new work item type:
    # 1. Subclass WorkItemHandler
    # 2. Implement: detect_complaints, should_handle_existing_complaint,
    #    create_new_complaint, handle_existing_complaint
    # 3. Register the type string here
    else:
        raise ValueError(f"Unsupported work item type: {work_item_type}")
