import React from 'react';
import {
  Button, Space, Tag, Modal, Form, Select, Input, message,
  Popconfirm, Card, Typography, Switch, Upload, Divider, Alert, Progress,
} from 'antd';
import { PlusOutlined, ReloadOutlined, UploadOutlined, DownloadOutlined, DownOutlined } from '@ant-design/icons';
import type { UploadFile } from 'antd/es/upload/interface';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import styles from './Subscriptions.module.css';
import { subscriptionService } from '../../services/subscriptions';
import { userService, User } from '../../services/users';
import { accountService, AWSAccount } from '../../services/accounts';
import ResponsiveList from '../../components/ResponsiveList';
import { postSSEStream, SSEMessage } from '../../lib/sseStream';

const { Text } = Typography;
const { Search } = Input;

const planLabels: Record<string, string> = {
  Q_DEVELOPER_STANDALONE_PRO: 'Kiro Pro',
  Q_DEVELOPER_STANDALONE_PRO_PLUS: 'Kiro Pro+',
  Q_DEVELOPER_STANDALONE_POWER: 'Kiro Power',
  KIRO_ENTERPRISE_PRO: 'Kiro Pro',
  KIRO_ENTERPRISE_PRO_PLUS: 'Kiro Pro+',
  KIRO_ENTERPRISE_PRO_POWER: 'Kiro Power',
};

// 计算用户生命周期状态
function getUserLifecycleStatus(user: User): 'unsubscribed' | 'email_unverified' | 'pending' | 'active' {
  const hasSub = user.has_subscription;
  const subStatus = (user.subscription_status || '').toUpperCase();

  if (!hasSub && !user.pending_subscription_type) {
    return 'unsubscribed';
  }
  if (hasSub || user.pending_subscription_type) {
    if (!user.email_verified) return 'email_unverified';
    if (subStatus === 'ACTIVE') return 'active';
    return 'pending';
  }
  return 'unsubscribed';
}

function getStatusTag(status: string, t: (key: string) => string) {
  switch (status) {
    case 'unsubscribed': return <Tag>{t('subscriptions.status.unsubscribed')}</Tag>;
    case 'email_unverified': return <Tag color="red">{t('subscriptions.status.emailUnverified')}</Tag>;
    case 'pending': return <Tag color="orange">{t('subscriptions.status.pending')}</Tag>;
    case 'active': return <Tag color="green">{t('subscriptions.status.active')}</Tag>;
    default: return <Tag>{status}</Tag>;
  }
}

// ========== Main Page ==========
const SubscriptionManagement: React.FC = () => {
  const { accountId: accountIdStr } = useParams<{ accountId: string }>();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const accountId = Number(accountIdStr);

  const [account, setAccount] = React.useState<AWSAccount | null>(null);
  const [users, setUsers] = React.useState<User[]>([]);
  const [total, setTotal] = React.useState(0);
  const [loading, setLoading] = React.useState(false);
  const [searchText, setSearchText] = React.useState('');
  const [statusFilter, setStatusFilter] = React.useState<string | undefined>(undefined);

  // Modals
  const [createModalVisible, setCreateModalVisible] = React.useState(false);
  const [csvModalVisible, setCsvModalVisible] = React.useState(false);
  const [changePlanModal, setChangePlanModal] = React.useState<User | null>(null);
  const [csvFile, setCsvFile] = React.useState<UploadFile | null>(null);
  const [csvSendEmail, setCsvSendEmail] = React.useState(true);
  const [batchLogs, setBatchLogs] = React.useState<string[]>([]);
  const [batchRunning, setBatchRunning] = React.useState(false);
  const [batchProgress, setBatchProgress] = React.useState<{ current: number; total: number; ok: number; failed: number }>({ current: 0, total: 0, ok: 0, failed: 0 });
  const [batchFailures, setBatchFailures] = React.useState<{ email: string; reason: string }[]>([]);
  const batchLogRef = React.useRef<HTMLDivElement>(null);

  // 多选相关状态
  const [selectedRowKeys, setSelectedRowKeys] = React.useState<React.Key[]>([]);
  const [batchChangePlanModalVisible, setBatchChangePlanModalVisible] = React.useState(false);

  const [createForm] = Form.useForm();
  const [changePlanForm] = Form.useForm();
  const [batchChangePlanForm] = Form.useForm();

  React.useEffect(() => {
    if (!accountId || isNaN(accountId)) { navigate('/accounts'); return; }
    accountService.get(accountId).then(setAccount).catch(() => navigate('/accounts'));
  }, [accountId, navigate]);

  const fetchUsers = React.useCallback(async () => {
    if (!accountId || isNaN(accountId)) return;
    setLoading(true);
    try {
      const data = await userService.listUsers(accountId, 0, 500, searchText || undefined, statusFilter);
      setUsers(data.users);
      setTotal(data.total);
    } catch { message.error('获取用户列表失败'); }
    finally { setLoading(false); }
  }, [accountId, searchText, statusFilter]);

  React.useEffect(() => { fetchUsers(); }, [fetchUsers]);

  // ===== Handlers =====
  const handleCreateUser = async (values: Record<string, any>) => {
    setCreateModalVisible(false);
    createForm.resetFields();
    try {
      await userService.createUser(accountId, {
        email: values.email,
        given_name: values.given_name || undefined,
        family_name: values.family_name || undefined,
        auto_subscribe: values.auto_subscribe ?? true,
        subscription_type: values.subscription_type,
        send_password_reset: values.send_password_reset ?? true,
      });
      message.success('用户已创建，后台正在处理邮件和订阅');
      fetchUsers();
    } catch (e: any) { message.error(e.response?.data?.detail || '创建失败'); }
  };

  const handleSubscribe = async (user: User) => {
    const subType = await new Promise<string | null>((resolve) => {
      Modal.confirm({
        title: `为 ${user.email} 分配订阅`,
        content: (
          <Select defaultValue="Q_DEVELOPER_STANDALONE_PRO" style={{ width: '100%', marginTop: 8 }}
            onChange={(v) => (window as any).__subType = v}>
            <Select.Option value="Q_DEVELOPER_STANDALONE_PRO">Kiro Pro</Select.Option>
            <Select.Option value="Q_DEVELOPER_STANDALONE_PRO_PLUS">Kiro Pro+</Select.Option>
            <Select.Option value="Q_DEVELOPER_STANDALONE_POWER">Kiro Power</Select.Option>
          </Select>
        ),
        onOk: () => resolve((window as any).__subType || 'Q_DEVELOPER_STANDALONE_PRO'),
        onCancel: () => resolve(null),
      });
    });
    if (!subType) return;
    try {
      await subscriptionService.create(accountId, {
        principal_id: user.user_id,
        subscription_type: subType,
      });
      message.success('订阅已分配');
      fetchUsers();
    } catch { message.error(t('subscriptions.subscribeFailed')); }
  };

  const handleChangePlan = async (values: { subscription_type: string }) => {
    if (!changePlanModal) return;
    try {
      const subs = await subscriptionService.list(accountId);
      const sub = subs.subscriptions.find(s => s.principal_id === changePlanModal.user_id);
      if (!sub) { message.error(t('subscriptions.subscribeFailed')); return; }
      await subscriptionService.update(accountId, sub.id, values.subscription_type);
      message.success(t('subscriptions.changePlanSuccess'));
      setChangePlanModal(null);
      changePlanForm.resetFields();
      fetchUsers();
    } catch (e: any) { message.error(e.response?.data?.detail || t('subscriptions.changePlanFailed')); }
  };

  const handleCancelSubscription = async (user: User) => {
    try {
      const subs = await subscriptionService.list(accountId);
      const sub = subs.subscriptions.find(s => s.principal_id === user.user_id);
      if (!sub) { message.error(t('subscriptions.cancelFailed')); return; }
      await subscriptionService.delete(accountId, sub.id);
      message.success(t('subscriptions.cancelSuccess'));
      fetchUsers();
    } catch (e: any) { message.error(e.response?.data?.detail || t('subscriptions.cancelFailed')); }
  };

  const handleResendEmail = async (userId: number) => {
    try {
      await userService.resetPassword(accountId, userId);
      message.success(t('subscriptions.resendSuccess'));
    } catch { message.error(t('subscriptions.resendFailed')); }
  };

  const handleResetPassword = async (userId: number) => {
    try {
      await userService.resetPassword(accountId, userId);
      message.success(t('subscriptions.resetPasswordSuccess'));
    } catch { message.error(t('subscriptions.resetPasswordFailed')); }
  };

  const handleDeleteUser = async (userId: number) => {
    try {
      await userService.deleteUser(accountId, userId);
      message.success(t('subscriptions.deleteSuccess'));
      fetchUsers();
    } catch (e: any) { message.error(e.response?.data?.detail || t('subscriptions.deleteFailed')); }
  };

  // CSV batch
  const handleCsvUpload = async () => {
    if (!csvFile?.originFileObj) return;
    setCsvModalVisible(false);
    const file = csvFile.originFileObj;
    setCsvFile(null);
    setBatchRunning(true);
    setBatchLogs(['⏳ 正在上传...']);
    setBatchProgress({ current: 0, total: 0, ok: 0, failed: 0 });
    setBatchFailures([]);
    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('send_password_reset', csvSendEmail ? 'true' : 'false');
      await postSSEStream(`/api/accounts/${accountId}/batch/users/csv/stream`, formData, handleBatchMessage);
    } catch (e: any) { setBatchLogs(prev => [...prev, `❌ ${e.message}`]); }
    finally { setBatchRunning(false); fetchUsers(); }
  };

  React.useEffect(() => {
    if (batchLogRef.current) batchLogRef.current.scrollTop = batchLogRef.current.scrollHeight;
  }, [batchLogs]);

  const downloadCsvTemplate = () => {
    const csv = 'email,subscription_type\nuser@example.com,Q_DEVELOPER_STANDALONE_PRO\n';
    const blob = new Blob([csv], { type: 'text/csv' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'users_template.csv'; a.click();
  };

  // 批量操作通用 SSE 执行器：复用底部「批量进度」Modal 显示实时进度
  const handleBatchMessage = React.useCallback((data: SSEMessage) => {
    setBatchProgress(prev => ({
      current: data.current ?? (data.type === 'done' ? prev.total : prev.current),
      total: data.total ?? prev.total,
      ok: data.success_count ?? prev.ok,
      failed: data.failed_count ?? prev.failed,
    }));
    if (data.failures && data.failures.length > 0) {
      setBatchFailures(data.failures);
    }
    if (data.message) {
      const time = new Date().toLocaleTimeString();
      setBatchLogs(prev => [...prev, `[${time}] ${data.message}`]);
    }
  }, []);

  // 下载失败明细 CSV，便于人工重试
  const downloadFailures = () => {
    const escape = (v: string) => `"${(v || '').replace(/"/g, '""')}"`;
    const header = 'email,reason\n';
    const rows = batchFailures.map(f => `${escape(f.email)},${escape(f.reason)}`).join('\n');
    // \uFEFF BOM 让 Excel 正确识别 UTF-8
    const blob = new Blob(['\uFEFF' + header + rows], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `batch_failures_${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const runBatchStream = async (url: string, body: Record<string, unknown>) => {
    setBatchRunning(true);
    setBatchLogs(['⏳ 正在启动批量操作...']);
    setBatchProgress({ current: 0, total: 0, ok: 0, failed: 0 });
    setBatchFailures([]);
    try {
      await postSSEStream(url, body, handleBatchMessage);
    } catch (e: any) {
      setBatchLogs(prev => [...prev, `❌ ${e.message}`]);
    } finally {
      setBatchRunning(false);
      setSelectedRowKeys([]);
      fetchUsers();
    }
  };

  // 批量变更套餐
  const handleBatchChangePlan = async (values: { subscription_type: string }) => {
    if (selectedRowKeys.length === 0) return;
    setBatchChangePlanModalVisible(false);
    batchChangePlanForm.resetFields();
    await runBatchStream(
      `/api/accounts/${accountId}/batch/change-plan/stream`,
      { user_ids: selectedRowKeys.map(Number), subscription_type: values.subscription_type },
    );
  };

  // 批量重置密码
  const handleBatchResetPassword = () => {
    if (selectedRowKeys.length === 0) return;
    Modal.confirm({
      title: t('subscriptions.batchResetPasswordTitle'),
      content: t('subscriptions.batchResetPasswordConfirm', { count: selectedRowKeys.length }),
      onOk: () => {
        // 不返回 Promise：确认框立即关闭，再由进度蒙版接管，避免双蒙版叠加
        runBatchStream(
          `/api/accounts/${accountId}/batch/reset-password/stream`,
          { user_ids: selectedRowKeys.map(Number), mode: 'email' },
        );
      },
    });
  };

  // 批量删除用户
  const handleBatchDelete = () => {
    if (selectedRowKeys.length === 0) return;
    Modal.confirm({
      title: t('subscriptions.batchDeleteTitle'),
      content: t('subscriptions.batchDeleteConfirm', { count: selectedRowKeys.length }),
      okButtonProps: { danger: true },
      onOk: () => {
        // 不返回 Promise：确认框立即关闭，再由进度蒙版接管，避免双蒙版叠加
        runBatchStream(
          `/api/accounts/${accountId}/batch/delete/stream`,
          { user_ids: selectedRowKeys.map(Number) },
        );
      },
    });
  };

  // 多选配置
  const rowSelection = {
    selectedRowKeys,
    onChange: (newSelectedRowKeys: React.Key[]) => {
      setSelectedRowKeys(newSelectedRowKeys);
    },
  };

  // ===== Columns =====
  const columns = [
    { title: t('common.email'), dataIndex: 'email', key: 'email', ellipsis: true, width: 180 },
    { title: t('common.username'), dataIndex: 'user_name', key: 'user_name', width: 90, ellipsis: true },
    {
      title: t('subscriptions.plan'), key: 'plan', width: 90,
      render: (_: unknown, r: User) => {
        const type = r.subscription_type || r.pending_subscription_type;
        return type ? <Tag color="blue">{planLabels[type] || type}</Tag> : '-';
      },
    },
    {
      title: t('common.status'), key: 'status', width: 100,
      render: (_: unknown, r: User) => getStatusTag(getUserLifecycleStatus(r), t),
    },
    {
      title: t('common.actions'), key: 'actions', width: 280,
      render: (_: unknown, record: User) => {
        const status = getUserLifecycleStatus(record);
        return (
          <Space wrap>
            {status === 'unsubscribed' && (
              <Button type="link" size="small" onClick={() => handleSubscribe(record)}>{t('subscriptions.actions.subscribe')}</Button>
            )}
            {(status === 'pending' || status === 'active') && (
              <>
                <Button type="link" size="small" onClick={() => {
                  setChangePlanModal(record);
                  changePlanForm.setFieldsValue({ subscription_type: record.subscription_type });
                }}>{t('subscriptions.actions.changePlan')}</Button>
                <Popconfirm title={t('subscriptions.cancelConfirm')} onConfirm={() => handleCancelSubscription(record)}>
                  <Button type="link" size="small" danger>{t('subscriptions.actions.cancelSubscription')}</Button>
                </Popconfirm>
              </>
            )}
            <Button type="link" size="small" onClick={() => handleResetPassword(record.id)}>{t('subscriptions.actions.resetPassword')}</Button>
            <Popconfirm
              title={t('subscriptions.deleteConfirm')}
              description={record.has_subscription ? t('subscriptions.deleteWithSubWarning') : t('subscriptions.deleteConfirmText')}
              onConfirm={() => handleDeleteUser(record.id)}
            >
              <Button type="link" size="small" danger>{t('subscriptions.actions.delete')}</Button>
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  return (
    <div className={styles.subscriptions}>
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space>
          <Text strong>{t('subscriptions.currentAccount')}:</Text>
          <Tag color="blue">{account?.name || '...'}</Tag>
          <Tag color={account?.status === 'active' ? 'green' : 'orange'}>{account?.status}</Tag>
        </Space>
      </Card>

      <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
        <Space wrap>
          <Search placeholder={t('subscriptions.searchPlaceholder')} style={{ width: 220 }} onSearch={setSearchText} allowClear />
          <Select placeholder={t('subscriptions.filterStatus')} allowClear style={{ width: 130 }} value={statusFilter} onChange={setStatusFilter}>
            <Select.Option value="UNVERIFIED">{t('subscriptions.status.emailUnverified')}</Select.Option>
            <Select.Option value="PENDING">{t('subscriptions.status.pending')}</Select.Option>
            <Select.Option value="ACTIVE">{t('subscriptions.status.active')}</Select.Option>
            <Select.Option value="NONE">{t('subscriptions.status.unsubscribed')}</Select.Option>
          </Select>
        </Space>
        <Space wrap>
          {selectedRowKeys.length > 0 && (
            <>
              <Button
                type="primary"
                onClick={() => setBatchChangePlanModalVisible(true)}
              >
                {t('subscriptions.batchChangePlan')} ({selectedRowKeys.length})
              </Button>
              <Button onClick={handleBatchResetPassword}>
                {t('subscriptions.batchResetPassword')} ({selectedRowKeys.length})
              </Button>
              <Button danger onClick={handleBatchDelete}>
                {t('subscriptions.batchDelete')} ({selectedRowKeys.length})
              </Button>
            </>
          )}
          <Button icon={<ReloadOutlined />} onClick={fetchUsers}>{t('common.refresh')}</Button>
          <Button icon={<UploadOutlined />} onClick={() => setCsvModalVisible(true)}>{t('subscriptions.csvImport')}</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateModalVisible(true)}>{t('subscriptions.addUser')}</Button>
        </Space>
      </div>

      <ResponsiveList
        columns={columns}
        dataSource={users}
        rowKey="id"
        loading={loading}
        rowSelection={rowSelection}
        pagination={{ total, pageSize: 500, showTotal: (n: number) => `${t('common.total', { count: n })}` }}
        scroll={{ x: 740 }}
      />

      {/* 创建用户 */}
      <Modal title={t('subscriptions.createUserTitle')} open={createModalVisible} onOk={() => createForm.submit()}
        onCancel={() => { setCreateModalVisible(false); createForm.resetFields(); }} width={480}>
        <Alert message={t('subscriptions.createUserHint')} type="info" showIcon style={{ marginBottom: 16 }} />
        <Form form={createForm} onFinish={handleCreateUser} layout="vertical"
          initialValues={{ auto_subscribe: true, subscription_type: 'Q_DEVELOPER_STANDALONE_PRO', send_password_reset: true }}>
          <Form.Item name="email" label={t('common.email')} rules={[{ required: true, type: 'email' }]}>
            <Input placeholder="user@example.com" />
          </Form.Item>
          <Space style={{ width: '100%' }}>
            <Form.Item name="given_name" label={t('subscriptions.givenName')} style={{ flex: 1 }}><Input placeholder={t('subscriptions.optional')} /></Form.Item>
            <Form.Item name="family_name" label={t('subscriptions.familyName')} style={{ flex: 1 }}><Input placeholder={t('subscriptions.optional')} /></Form.Item>
          </Space>
          <Form.Item name="auto_subscribe" label={t('subscriptions.autoSubscribe')} valuePropName="checked"><Switch /></Form.Item>
          <Form.Item noStyle shouldUpdate={(p, c) => p.auto_subscribe !== c.auto_subscribe}>
            {({ getFieldValue }) => getFieldValue('auto_subscribe') ? (
              <Form.Item name="subscription_type" label={t('subscriptions.plan')}>
                <Select>
                  <Select.Option value="Q_DEVELOPER_STANDALONE_PRO">{t('subscriptions.plans.pro')}</Select.Option>
                  <Select.Option value="Q_DEVELOPER_STANDALONE_PRO_PLUS">Kiro Pro+</Select.Option>
                  <Select.Option value="Q_DEVELOPER_STANDALONE_POWER">Kiro Power</Select.Option>
                </Select>
              </Form.Item>
            ) : null}
          </Form.Item>
          <Form.Item name="send_password_reset" label={t('subscriptions.sendPasswordReset')} valuePropName="checked"><Switch /></Form.Item>
        </Form>
      </Modal>

      {/* 变更套餐 */}
      <Modal title={t('subscriptions.changePlanTitle')} open={!!changePlanModal} onOk={() => changePlanForm.submit()}
        onCancel={() => { setChangePlanModal(null); changePlanForm.resetFields(); }}>
        <p>{t('common.email')}: {changePlanModal?.email}</p>
        <Form form={changePlanForm} onFinish={handleChangePlan} layout="vertical">
          <Form.Item name="subscription_type" label={t('subscriptions.plan')} rules={[{ required: true }]}>
            <Select>
              <Select.Option value="Q_DEVELOPER_STANDALONE_PRO">{t('subscriptions.plans.pro')}</Select.Option>
              <Select.Option value="Q_DEVELOPER_STANDALONE_PRO_PLUS">{t('subscriptions.plans.proPlus')}</Select.Option>
              <Select.Option value="Q_DEVELOPER_STANDALONE_POWER">{t('subscriptions.plans.power')}</Select.Option>
            </Select>
          </Form.Item>
        </Form>
      </Modal>

      {/* 批量变更套餐 */}
      <Modal
        title={t('subscriptions.batchChangePlanTitle')}
        open={batchChangePlanModalVisible}
        onOk={() => batchChangePlanForm.submit()}
        onCancel={() => { setBatchChangePlanModalVisible(false); batchChangePlanForm.resetFields(); }}
      >
        <Alert
          message={t('subscriptions.batchChangePlanHint', { count: selectedRowKeys.length })}
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
        />
        <Form form={batchChangePlanForm} onFinish={handleBatchChangePlan} layout="vertical"
          initialValues={{ subscription_type: 'Q_DEVELOPER_STANDALONE_PRO' }}>
          <Form.Item name="subscription_type" label={t('subscriptions.plan')} rules={[{ required: true }]}>
            <Select>
              <Select.Option value="Q_DEVELOPER_STANDALONE_PRO">{t('subscriptions.plans.pro')}</Select.Option>
              <Select.Option value="Q_DEVELOPER_STANDALONE_PRO_PLUS">{t('subscriptions.plans.proPlus')}</Select.Option>
              <Select.Option value="Q_DEVELOPER_STANDALONE_POWER">{t('subscriptions.plans.power')}</Select.Option>
            </Select>
          </Form.Item>
        </Form>
      </Modal>

      {/* CSV 导入 */}
      <Modal title={t('subscriptions.csvTitle')} open={csvModalVisible} onOk={handleCsvUpload}
        onCancel={() => { setCsvModalVisible(false); setCsvFile(null); }}
        okText={t('common.confirm')} okButtonProps={{ disabled: !csvFile || batchRunning }}>
        <Alert message={<>{t('subscriptions.csvHint')}</>} type="info" showIcon style={{ marginBottom: 16 }} />
        <Button icon={<DownloadOutlined />} onClick={downloadCsvTemplate} type="dashed" block>{t('subscriptions.downloadTemplate')}</Button>
        <Divider />
        <div style={{ marginBottom: 12 }}>
          <Space>
            <Switch checked={csvSendEmail} onChange={setCsvSendEmail} />
            <span>{t('subscriptions.sendPasswordReset')}</span>
          </Space>
        </div>
        <Upload accept=".csv" maxCount={1} beforeUpload={() => false}
          fileList={csvFile ? [csvFile] : []} onChange={({ fileList }) => setCsvFile(fileList[0] || null)}>
          <Button icon={<UploadOutlined />} block>{t('subscriptions.selectCsv')}</Button>
        </Upload>
      </Modal>

      {/* 批量进度 */}
      <Modal title={t('subscriptions.batchProgress')} open={batchRunning || batchLogs.length > 0}
        closable={!batchRunning} maskClosable={false}
        footer={batchRunning ? null : (
          <Space>
            {batchFailures.length > 0 && (
              <Button danger icon={<DownloadOutlined />} onClick={downloadFailures}>
                下载失败列表 ({batchFailures.length})
              </Button>
            )}
            <Button type="primary" onClick={() => { setBatchLogs([]); setBatchProgress({ current: 0, total: 0, ok: 0, failed: 0 }); setBatchFailures([]); }}>{t('common.close')}</Button>
          </Space>
        )} width={640}>
        {batchProgress.total > 0 && (
          <div style={{ marginBottom: 12 }}>
            <Progress
              percent={Math.round((batchProgress.current / batchProgress.total) * 100)}
              status={batchRunning ? 'active' : (batchProgress.failed > 0 ? 'exception' : 'success')}
            />
            <Space size="small" style={{ fontSize: 13 }}>
              <span>{batchProgress.current}/{batchProgress.total}</span>
              {!batchRunning && <Tag color="green">✅ {batchProgress.ok}</Tag>}
              {!batchRunning && batchProgress.failed > 0 && <Tag color="red">❌ {batchProgress.failed}</Tag>}
            </Space>
          </div>
        )}
        {!batchRunning && batchFailures.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`有 ${batchFailures.length} 个用户处理失败，可下载失败列表后手动重试。`}
          />
        )}
        <div ref={batchLogRef} style={{
          background: '#1a1a1a', color: '#e0e0e0', padding: 12, borderRadius: 6,
          fontFamily: 'monospace', fontSize: 13, lineHeight: 1.6, maxHeight: 400, overflowY: 'auto', whiteSpace: 'pre-wrap',
        }}>
          {batchLogs.map((log, i) => <div key={i}>{log}</div>)}
          {batchRunning && <div style={{ color: '#888' }}>⏳ {t('subscriptions.processing')}</div>}
        </div>
      </Modal>
    </div>
  );
};

export default SubscriptionManagement;
