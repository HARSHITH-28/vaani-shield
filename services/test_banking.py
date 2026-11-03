
from services.banking_service import (
    get_customer_profile,
    get_account_balance,
    get_recent_transactions,
    search_transactions,
    get_card_details,
    get_loan_details,
    calculate_emi
)

CUSTOMER_ID = 1

print("\n===== VAANISHIELD BANKING SERVICE TEST =====")

print("\n1. CUSTOMER PROFILE")
print(get_customer_profile(CUSTOMER_ID))

print("\n2. ACCOUNT BALANCE")
print(get_account_balance(CUSTOMER_ID))

print("\n3. RECENT TRANSACTIONS")
for txn in get_recent_transactions(CUSTOMER_ID, 5):
    print(txn)

print("\n4. TRANSACTIONS ABOVE ₹1000")
for txn in search_transactions(CUSTOMER_ID, min_amount=1000):
    print(txn)

print("\n5. CARD DETAILS")
print(get_card_details(CUSTOMER_ID))

print("\n6. LOAN DETAILS")
print(get_loan_details(CUSTOMER_ID))

print("\n7. EMI CALCULATOR")
emi = calculate_emi(500000, 9.5, 60)
print(f"₹5,00,000 loan EMI at 9.5% for 60 months: ₹{emi}")

print("\n===== TEST COMPLETED =====")
