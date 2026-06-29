"""Identity Center operations with multi-account support."""
import os
from typing import Dict, List, Optional, Any
from .base_client import AWSClient


class IdentityCenterClient:
    """Identity Center client for user and group management."""
    
    def __init__(self, aws_client: AWSClient, sso_region: str = "us-east-2"):
        """Initialize Identity Center client."""
        self.aws_client = aws_client
        self.sso_region = sso_region
        self._identitystore = None
        self._sso_admin = None
    
    def _get_identitystore_client(self):
        """Get Identity Store boto3 client."""
        if self._identitystore is None:
            self._identitystore = self.aws_client.get_boto3_client(
                "identitystore",
                region=self.sso_region
            )
        return self._identitystore
    
    def _get_sso_admin_client(self):
        """Get SSO Admin boto3 client."""
        if self._sso_admin is None:
            self._sso_admin = self.aws_client.get_boto3_client(
                "sso-admin",
                region=self.sso_region
            )
        return self._sso_admin
    
    def search_users_with_verification(self, identity_store_id: str) -> List[Dict[str, Any]]:
        """
        使用内部 API SearchUsers 获取用户列表（含邮箱验证状态），支持分页.
        
        URL: identitystore.{sso_region}.amazonaws.com/identitystore/
        Target: AWSIdentityStoreService.SearchUsers
        Signing: service=identitystore, region=sso_region

        Raises:
            RuntimeError: 当内部 API 调用失败时抛出，调用方应回退到标准 list_users。
            注意：绝不能在失败时静默返回空列表，否则会被误判为"账号无用户"，
            导致同步时清空本地用户数据或重置邮箱验证状态。
        """
        all_users = []
        next_token = None
        
        while True:
            payload = {
                "IdentityStoreId": identity_store_id,
                "Filters": [],
                "MaxResults": 100,
            }
            if next_token:
                payload["NextToken"] = next_token
            
            resp = self.aws_client.sigv4_post(
                url=f"https://identitystore.{self.sso_region}.amazonaws.com/identitystore/",
                target="AWSIdentityStoreService.SearchUsers",
                payload=payload,
                service="identitystore",
                region=self.sso_region,
            )
            
            if resp.status_code != 200:
                raise RuntimeError(
                    f"SearchUsers failed: HTTP {resp.status_code} {resp.text[:200]}"
                )
            
            data = resp.json()
            
            for user in data.get("Users", []):
                attrs = user.get("UserAttributes", {})
                emails_data = attrs.get("emails", {}).get("ComplexListValue", [])
                
                primary_email = ""
                email_verified = False
                for email_entry in emails_data:
                    value = email_entry.get("value", {}).get("StringValue", "")
                    is_primary = email_entry.get("primary", {}).get("BooleanValue", False)
                    verification = email_entry.get("verificationStatus", {}).get("StringValue", "NOT_VERIFIED")
                    
                    if is_primary or not primary_email:
                        primary_email = value
                        email_verified = verification == "VERIFIED"
                
                name_data = attrs.get("name", {}).get("ComplexValue", {})
                
                all_users.append({
                    "UserId": user.get("UserId", ""),
                    "UserName": user.get("UserName", ""),
                    "DisplayName": attrs.get("displayName", {}).get("StringValue", ""),
                    "Email": primary_email,
                    "GivenName": name_data.get("givenName", {}).get("StringValue", ""),
                    "FamilyName": name_data.get("familyName", {}).get("StringValue", ""),
                    "Status": self._derive_status(user),
                    "UserStatus": user.get("UserStatus", ""),
                    "EmailVerified": email_verified,
                })
            
            # 检查是否有下一页
            next_token = data.get("NextToken")
            if not next_token:
                break
        
        return all_users

    @staticmethod
    def _derive_status(user: Dict[str, Any]) -> str:
        """
        统一推导用户启用/禁用状态。

        优先使用 'Active' 布尔字段；其次使用 'UserStatus'；
        都缺失时默认 'enabled'（与 list_users / get_user_by_id 保持一致，
        AWS Identity Center 用户默认即为启用状态）。
        """
        active = user.get("Active")
        if active is not None:
            return "enabled" if active else "disabled"

        user_status = (user.get("UserStatus") or "").upper()
        if user_status:
            disabled_states = {"DISABLED", "INACTIVE", "SUSPENDED"}
            return "disabled" if user_status in disabled_states else "enabled"

        return "enabled"
    
    def get_instance_info(self, instance_arn: Optional[str] = None) -> Dict[str, str]:
        """
        Get Identity Center instance information.
        
        Returns:
            dict with 'instance_arn' and 'identity_store_id'
        """
        sso = self._get_sso_admin_client()
        instances = sso.list_instances()["Instances"]
        
        if not instances:
            raise RuntimeError("No Identity Center instance found")
        
        if instance_arn:
            for inst in instances:
                if inst["InstanceArn"] == instance_arn:
                    return {
                        "instance_arn": inst["InstanceArn"],
                        "identity_store_id": inst["IdentityStoreId"]
                    }
            raise RuntimeError(f"Instance not found: {instance_arn}")
        
        inst = instances[0]
        return {
            "instance_arn": inst["InstanceArn"],
            "identity_store_id": inst["IdentityStoreId"]
        }
    
    def create_user(
        self,
        identity_store_id: str,
        username: str,
        display_name: str,
        given_name: str,
        family_name: str,
        email: str
    ) -> str:
        """
        Create an Identity Center user.
        
        Returns:
            The created UserId
        """
        identitystore = self._get_identitystore_client()
        
        resp = identitystore.create_user(
            IdentityStoreId=identity_store_id,
            UserName=username,
            Name={"GivenName": given_name, "FamilyName": family_name},
            DisplayName=display_name,
            Emails=[{"Value": email, "Type": "Work", "Primary": True}],
        )
        return resp["UserId"]
    
    def delete_user(self, identity_store_id: str, user_id: str) -> bool:
        """Delete an Identity Center user."""
        identitystore = self._get_identitystore_client()
        try:
            identitystore.delete_user(
                IdentityStoreId=identity_store_id,
                UserId=user_id
            )
            return True
        except Exception:
            return False
    
    def get_user_by_id(self, identity_store_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Get user by ID.
        
        Returns:
            User info dict or None
        """
        identitystore = self._get_identitystore_client()
        try:
            resp = identitystore.describe_user(
                IdentityStoreId=identity_store_id,
                UserId=user_id,
            )
            
            primary_email = next(
                (e["Value"] for e in resp.get("Emails", []) if e.get("Primary")),
                resp.get("Emails", [{}])[0].get("Value", "") if resp.get("Emails") else "",
            )
            
            return {
                "UserId": resp.get("UserId", ""),
                "UserName": resp.get("UserName", ""),
                "DisplayName": resp.get("DisplayName", ""),
                "Email": primary_email,
                "GivenName": resp.get("Name", {}).get("GivenName", ""),
                "FamilyName": resp.get("Name", {}).get("FamilyName", ""),
                "Status": resp.get("Active", True) and "enabled" or "disabled"
            }
        except Exception:
            return None
    
    def find_user_by_email(self, identity_store_id: str, email: str) -> Optional[str]:
        """
        Find user by email.
        
        Returns:
            UserId or None
        """
        identitystore = self._get_identitystore_client()
        paginator = identitystore.get_paginator("list_users")
        
        for page in paginator.paginate(IdentityStoreId=identity_store_id):
            for user in page["Users"]:
                for e in user.get("Emails", []):
                    if e.get("Value", "").lower() == email.lower():
                        return user["UserId"]
        return None
    
    def list_users(self, identity_store_id: str) -> List[Dict[str, Any]]:
        """
        List all users in Identity Center.
        
        Returns:
            List of user info dicts
        """
        identitystore = self._get_identitystore_client()
        paginator = identitystore.get_paginator("list_users")
        
        users = []
        for page in paginator.paginate(IdentityStoreId=identity_store_id):
            for user in page["Users"]:
                primary_email = next(
                    (e["Value"] for e in user.get("Emails", []) if e.get("Primary")),
                    user.get("Emails", [{}])[0].get("Value", "") if user.get("Emails") else "",
                )
                users.append({
                    "UserId": user.get("UserId", ""),
                    "UserName": user.get("UserName", ""),
                    "DisplayName": user.get("DisplayName", ""),
                    "Email": primary_email,
                    "GivenName": user.get("Name", {}).get("GivenName", ""),
                    "FamilyName": user.get("Name", {}).get("FamilyName", ""),
                    "Status": "enabled" if user.get("Active", True) else "disabled"
                })
        
        return users
    
    def send_password_reset_email(self, user_id: str) -> Dict[str, Any]:
        """Send password reset email."""
        resp = self.aws_client.sigv4_post(
            url=f"https://identitystore.{self.sso_region}.amazonaws.com/",
            target="SWBUPService.UpdatePassword",
            payload={"UserId": user_id, "PasswordMode": "EMAIL"},
            service="userpool",
            region=self.sso_region,
        )
        return {
            "success": resp.status_code == 200,
            "status_code": resp.status_code,
            "message": resp.text if resp.status_code != 200 else "Password reset email sent"
        }
    
    def send_password_reset_otp(self, user_id: str) -> Dict[str, Any]:
        """Send password reset OTP."""
        resp = self.aws_client.sigv4_post(
            url=f"https://identitystore.{self.sso_region}.amazonaws.com/",
            target="SWBUPService.UpdatePassword",
            payload={"UserId": user_id, "PasswordMode": "OTP"},
            service="userpool",
            region=self.sso_region,
        )
        return {
            "success": resp.status_code == 200,
            "status_code": resp.status_code,
            "message": resp.text if resp.status_code != 200 else "OTP sent"
        }
    
    def send_email_verification(self, user_id: str, identity_store_id: str) -> Dict[str, Any]:
        """Send email verification."""
        resp = self.aws_client.sigv4_post(
            url=f"https://pvs-controlplane.{self.sso_region}.prod.authn.identity.aws.dev/",
            target="AWSPasswordControlPlaneService.StartEmailVerification",
            payload={"UserId": user_id, "IdentityStoreId": identity_store_id},
            service="sso-directory",
            region=self.sso_region,
        )
        return {
            "success": resp.status_code == 200,
            "status_code": resp.status_code,
            "message": resp.text if resp.status_code != 200 else "Verification email sent"
        }
    
    def list_groups(self, identity_store_id: str) -> List[Dict[str, Any]]:
        """List all groups."""
        identitystore = self._get_identitystore_client()
        resp = identitystore.list_groups(IdentityStoreId=identity_store_id)
        return resp.get("Groups", [])
    
    def get_group_id_by_name(self, identity_store_id: str, group_name: str) -> Optional[str]:
        """Get group ID by name."""
        identitystore = self._get_identitystore_client()
        try:
            resp = identitystore.get_group_id(
                IdentityStoreId=identity_store_id,
                AlternateIdentifier={
                    "UniqueAttribute": {
                        "AttributePath": "displayName",
                        "AttributeValue": group_name,
                    }
                },
            )
            return resp.get("GroupId")
        except Exception:
            return None
    
    def add_user_to_group(
        self,
        identity_store_id: str,
        user_id: str,
        group_id: str
    ) -> str:
        """Add user to group. Returns MembershipId."""
        identitystore = self._get_identitystore_client()
        resp = identitystore.create_group_membership(
            IdentityStoreId=identity_store_id,
            GroupId=group_id,
            MemberId={"UserId": user_id},
        )
        return resp["MembershipId"]
