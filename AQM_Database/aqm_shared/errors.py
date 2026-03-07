class AQMDatabaseError(Exception):
    pass

class VaultUnavailableError(AQMDatabaseError):
    def __init__(self , message):
        message = f"Vault_error  = {message}"
        super().__init__(message)

class InventoryUnavailableError(AQMDatabaseError):
    def __init__(self , message):
        message = f"Inventory_error  = {message}"
        super().__init__(message)

class KeyAlreadyExistsError(AQMDatabaseError):
    def __init__(self , key):
        self.key = key
        message = f"Key {key} already exists"
        super().__init__(message)

class InvalidCoinCategoryError(AQMDatabaseError):
    def __init__(self , category):
        self.category = category
        message = f"Invalid Coin Category {category}"
        super().__init__(message)

class KeyNotFoundError(AQMDatabaseError):
    def __init__(self , key):
        self.key = key
        message = f"Key {key} not found"
        super().__init__(message)

class KeyAlreadyBurnedError(AQMDatabaseError):
    def __init__(self , key):
        self.key = key
        message = f"Key {key} already burned"
        super().__init__(message)


class InvalidPriorityError(AQMDatabaseError):
    def __init__(self , priority):
        self.priority = priority
        message = f"Invalid Priority {priority}"
        super().__init__(message)


class ContactNotRegisteredError(AQMDatabaseError):
    def __init__(self , contact_id):
        self.contact = contact_id
        message = f"Contact {contact_id} not registered"
        super().__init__(message)


class BudgetExceededError(AQMDatabaseError):
    def __init__(self, contact_id, coin_category, current_count, cap):
        self.contact_id = contact_id
        self.coin_category = coin_category
        self.current_count = current_count
        self.cap = cap
        message = f"Budget exceeded for {contact_id}/{coin_category}: {current_count}/{cap}"
        super().__init__(message)


class ConcurrencyError(AQMDatabaseError):
    def __init__(self, operation):
        message = f"Optimistic lock failed after max retries: {operation}"
        super().__init__(message)

class ServerDatabaseError(Exception):
    pass

class UploadError(ServerDatabaseError):
    def __init__(self , message):
        super().__init__(message)

class ConnectionPoolError(ServerDatabaseError):
    def __init__(self , message):
        super().__init__(message)

class FetchError(ServerDatabaseError):
    def __init__(self , message):
        super().__init__(message)