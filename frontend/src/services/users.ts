import api from '../lib/api';

export interface User {
  id: number;
  user_id: string;
  user_name: string;
  display_name: string | null;
  email: string;
  given_name: string | null;
  family_name: string | null;
  status: string;
  groups: Record<string, unknown>[] | null;
  has_subscription: boolean;
  subscription_type: string | null;
  subscription_status: string | null;
  pending_subscription_type: string | null;
  email_verified: boolean;
  last_synced: string | null;
  created_at: string;
}

export interface UserListResponse {
  total: number;
  users: User[];
}

export interface CreateUserRequest {
  email: string;
  given_name?: string;
  family_name?: string;
  display_name?: string;
  user_name?: string;
  auto_subscribe?: boolean;
  subscription_type?: string;
  send_password_reset?: boolean;
}

export interface BatchActionResult {
  user_id: number;
  email: string | null;
  success: boolean;
  message: string;
}

export interface BatchActionResponse {
  total: number;
  success_count: number;
  failed_count: number;
  results: BatchActionResult[];
}

export const userService = {
  async listUsers(
    accountId: number,
    skip = 0,
    limit = 200,
    search?: string,
    subscriptionStatus?: string,
    sortBy?: string,
    sortOrder?: string,
  ): Promise<UserListResponse> {
    const response = await api.get<UserListResponse>(`/accounts/${accountId}/users`, {
      params: {
        skip, limit, search,
        subscription_status: subscriptionStatus,
        sort_by: sortBy,
        sort_order: sortOrder,
      },
    });
    return response.data;
  },

  async createUser(accountId: number, data: CreateUserRequest): Promise<User> {
    const response = await api.post<User>(`/accounts/${accountId}/users`, data);
    return response.data;
  },

  async getUser(accountId: number, userId: number): Promise<User> {
    const response = await api.get<User>(`/accounts/${accountId}/users/${userId}`);
    return response.data;
  },

  async deleteUser(accountId: number, userId: number): Promise<void> {
    await api.delete(`/accounts/${accountId}/users/${userId}`);
  },

  async resetPassword(accountId: number, userId: number, mode: 'email' | 'otp' = 'email'): Promise<{ success: boolean; message: string }> {
    const response = await api.post(`/accounts/${accountId}/users/${userId}/reset-password`, { mode });
    return response.data;
  },

  async sendVerificationEmail(accountId: number, userId: number): Promise<{ success: boolean; message: string }> {
    const response = await api.post(`/accounts/${accountId}/users/${userId}/verify-email`);
    return response.data;
  },

  async batchResetPassword(
    accountId: number,
    userIds: number[],
    mode: 'email' | 'otp' = 'email'
  ): Promise<BatchActionResponse> {
    const response = await api.post<BatchActionResponse>(
      `/accounts/${accountId}/users/batch-reset-password`,
      { user_ids: userIds, mode }
    );
    return response.data;
  },

  async batchDeleteUsers(
    accountId: number,
    userIds: number[]
  ): Promise<BatchActionResponse> {
    const response = await api.post<BatchActionResponse>(
      `/accounts/${accountId}/users/batch-delete`,
      { user_ids: userIds }
    );
    return response.data;
  },

  async batchCreateUsers(
    accountId: number,
    users: Array<{
      email: string;
      given_name: string;
      family_name: string;
      display_name?: string;
      user_name?: string;
      subscription_type?: string;
    }>,
    sendPasswordReset = true
  ): Promise<{
    total: number;
    success_count: number;
    failed_count: number;
    results: Array<{ email: string; success: boolean; message: string }>;
  }> {
    const response = await api.post(`/accounts/${accountId}/batch/users`, {
      users,
      send_password_reset: sendPasswordReset,
    });
    return response.data;
  },

  async batchCreateUsersCSV(
    _accountId: number,
    _file: File,
    _sendPasswordReset = true
  ): Promise<void> {
    // This now uses SSE streaming - handled directly in the component
    throw new Error('Use SSE streaming endpoint instead');
  },
};
