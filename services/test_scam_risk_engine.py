"""Offline tests for the deterministic scam-risk analysis service."""

import unittest

from services.scam_risk_engine import (
    EvidenceStrength,
    RiskLevel,
    analyze_scam_text,
)


class ScamRiskEngineTests(unittest.TestCase):
    def test_english_otp_scam_combines_impersonation_threat_and_urgency(self):
        result = analyze_scam_text(
            "I received this SMS: We are calling from your bank. Your account "
            "will be blocked in 10 minutes unless you send your OTP now."
        )
        self.assertEqual(result.risk_level, RiskLevel.CRITICAL)
        self.assertEqual(result.evidence_strength, EvidenceStrength.STRONG)
        categories = {item.category for item in result.indicators}
        self.assertTrue({"credential_request", "authority_impersonation", "account_threat"}.issubset(categories))
        self.assertIn("send your OTP", " ".join(result.indicators[0].evidence))
        self.assertTrue(any("Do not share" in item for item in result.recommended_steps))

    def test_account_blocking_threat_with_otp_request_is_never_low(self):
        result = analyze_scam_text(
            "Your account will be blocked today. Reply with your OTP to prevent it."
        )
        self.assertIn(result.risk_level, {RiskLevel.HIGH, RiskLevel.CRITICAL})
        categories = {item.category for item in result.indicators}
        self.assertIn("credential_request", categories)
        self.assertIn("account_threat", categories)

    def test_kyc_phishing_and_unverified_link_are_explained(self):
        result = analyze_scam_text(
            "Update your KYC now or your account will be blocked. Click https://example.invalid/verify"
        )
        self.assertIn(result.risk_level, {RiskLevel.HIGH, RiskLevel.CRITICAL})
        categories = {item.category for item in result.indicators}
        self.assertIn("kyc_threat", categories)
        self.assertIn("suspicious_link", categories)
        self.assertTrue(all(item.evidence for item in result.indicators))

    def test_legitimate_transaction_notification_is_not_automatically_a_scam(self):
        result = analyze_scam_text(
            "Your debit card purchase of INR 450 at Metro Store is approved."
        )
        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(result.indicators, ())

    def test_ambiguous_bank_contact_remains_low_with_caveat(self):
        result = analyze_scam_text("Your bank may contact you about your account.")
        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertIn("not proof", result.explanation)

    def test_upi_pin_request_is_high_without_escalating_from_keyword_alone(self):
        result = analyze_scam_text(
            "A bank support agent asked me to tell them my UPI PIN."
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertIn("credential_request", {item.category for item in result.indicators})
        self.assertFalse(any(item.severity == RiskLevel.CRITICAL for item in result.indicators))

    def test_bank_impersonation_and_account_block_threat_are_critical_with_otp_ask(self):
        result = analyze_scam_text(
            "The caller said: I am an RBI officer. Your account will be blocked "
            "unless you share the OTP immediately."
        )
        self.assertEqual(result.risk_level, RiskLevel.CRITICAL)
        self.assertIn("authority_impersonation", {item.category for item in result.indicators})
        self.assertIn("account_threat", {item.category for item in result.indicators})

    def test_remote_access_app_request_is_high(self):
        result = analyze_scam_text(
            "I got a call from bank support asking me to install AnyDesk and share my screen."
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertIn("remote_access_request", {item.category for item in result.indicators})
        self.assertTrue(any("remote-access" in item for item in result.recommended_steps))

    def test_payment_pressure_is_high(self):
        result = analyze_scam_text("Transfer INR 5000 right now or your service will stop.")
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertIn("payment_request", {item.category for item in result.indicators})
        self.assertIn("urgency_or_intimidation", {item.category for item in result.indicators})

    def test_prize_fee_and_urgency_are_critical(self):
        result = analyze_scam_text(
            "You won a lottery prize. Pay the release fee immediately to receive it."
        )
        self.assertEqual(result.risk_level, RiskLevel.CRITICAL)
        self.assertIn("upfront_fee_request", {item.category for item in result.indicators})

    def test_fake_support_refund_fee_is_high(self):
        result = analyze_scam_text(
            "A customer support agent says I must pay a processing fee to release my refund."
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertIn("support_impersonation_context", {item.category for item in result.indicators})
        self.assertIn("upfront_fee_request", {item.category for item in result.indicators})

    def test_government_impersonation_with_payment_threat_is_assessed(self):
        result = analyze_scam_text(
            "A police officer says pay the fine immediately or you will be arrested."
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertIn("authority_impersonation", {item.category for item in result.indicators})

    def test_prize_link_without_payment_is_medium(self):
        result = analyze_scam_text(
            "You won a prize; click this link to learn how to claim it."
        )
        self.assertEqual(result.risk_level, RiskLevel.MEDIUM)
        self.assertEqual(result.evidence_strength, EvidenceStrength.LIMITED)

    def test_kannada_contextual_credential_scam(self):
        result = analyze_scam_text(
            "ಬ್ಯಾಂಕ್ ಅಧಿಕಾರಿ ಎಂದು ಹೇಳಿ ನಿಮ್ಮ ಖಾತೆ ಬ್ಲಾಕ್ ಆಗುತ್ತದೆ, ಒಟಿಪಿ ಕಳುಹಿಸಿ."
        )
        self.assertEqual(result.risk_level, RiskLevel.CRITICAL)
        self.assertIn("kn-IN", result.detected_languages)
        self.assertIn("credential_request", {item.category for item in result.indicators})

    def test_hindi_contextual_credential_scam(self):
        result = analyze_scam_text(
            "बैंक अधिकारी कहता है कि आपका खाता ब्लॉक होगा, अभी अपना ओटीपी बताएं।"
        )
        self.assertEqual(result.risk_level, RiskLevel.CRITICAL)
        self.assertIn("hi-IN", result.detected_languages)
        self.assertIn("credential_request", {item.category for item in result.indicators})

    def test_kannada_english_code_mixed_kyc_message(self):
        result = analyze_scam_text(
            "ನಿಮ್ಮ KYC update ಮಾಡಿ ಇಲ್ಲದಿದ್ದರೆ account block ಆಗುತ್ತದೆ, OTP ಕಳುಹಿಸಿ."
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertTrue(result.is_code_mixed)
        self.assertIn("kn-IN", result.detected_languages)
        self.assertIn("en-IN", result.detected_languages)

    def test_hindi_english_code_mixed_message_with_language_metadata(self):
        result = analyze_scam_text(
            "Bank se bol raha hoon, abhi apna OTP batao warna account block hoga.",
            detected_languages=["hi-IN", "en-IN"],
        )
        self.assertEqual(result.risk_level, RiskLevel.CRITICAL)
        self.assertTrue(result.is_code_mixed)
        self.assertIn("credential_request", {item.category for item in result.indicators})

    def test_benign_bank_app_otp_instruction_is_low(self):
        result = analyze_scam_text(
            "To sign in, enter the OTP in the official bank app. Never share it with anyone."
        )
        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(result.indicators, ())

    def test_person_warning_about_otp_scams_is_low(self):
        result = analyze_scam_text(
            "Never share your OTP or UPI PIN. Banks do not ask for those codes."
        )
        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(result.indicators, ())

    def test_negated_quoted_phrase_is_not_treated_as_an_active_request(self):
        result = analyze_scam_text(
            "I will not send my OTP to anyone, and I did not install AnyDesk."
        )
        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(result.indicators, ())

    def test_received_quoted_scam_message_remains_assessable(self):
        result = analyze_scam_text(
            'I received this message: "Your account will be blocked; send your OTP now."'
        )
        self.assertIn(result.risk_level, {RiskLevel.HIGH, RiskLevel.CRITICAL})
        self.assertTrue(any("send your OTP" in evidence for item in result.indicators for evidence in item.evidence))

    def test_educational_discussion_is_not_assessed_as_an_attack(self):
        result = analyze_scam_text(
            "How do scammers trick people into sharing OTPs? They ask you to share your OTP."
        )
        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(result.indicators, ())

    def test_empty_malformed_and_oversized_inputs_return_unknown(self):
        for value in ("", "   ", None, 7, "\ud800", "x" * 12_001):
            with self.subTest(value_type=type(value).__name__):
                result = analyze_scam_text(value)
                self.assertEqual(result.risk_level, RiskLevel.UNKNOWN)
                self.assertEqual(result.evidence_strength, EvidenceStrength.NONE)

    def test_unsupported_script_returns_unknown_without_inventing_assessment(self):
        result = analyze_scam_text("مرحبا، أرسل كلمة المرور")
        self.assertEqual(result.risk_level, RiskLevel.UNKNOWN)
        self.assertEqual(result.language_status, "unsupported")
        self.assertEqual(result.indicators, ())

    def test_uncertain_case_does_not_overstate_risk(self):
        result = analyze_scam_text("The bank called me about my account.")
        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertIn("not proof", result.explanation)

    def test_conflicting_suspicious_and_protective_sentences_retain_suspicious_evidence(self):
        result = analyze_scam_text(
            "This message asks you to send your OTP now. Do not share your OTP."
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertEqual(len(result.indicators), 1)
        self.assertIn("send your OTP", result.indicators[0].evidence[0])

    def test_result_is_deterministic_and_json_friendly(self):
        text = "We are from your bank. Your account will be blocked unless you send OTP now."
        first = analyze_scam_text(text).to_dict()
        second = analyze_scam_text(text).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(first["risk_level"], "CRITICAL")
        self.assertIsInstance(first["indicators"], list)
        self.assertEqual(first["engine_version"], "1.0.0")

    def test_malformed_language_metadata_is_ignored_safely(self):
        result = analyze_scam_text("Please share your OTP.", detected_languages=object())
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertEqual(result.detected_languages, ("en-IN",))


if __name__ == "__main__":
    unittest.main()
