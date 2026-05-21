import { useState, useEffect } from 'react'
import {
  Card, Button, Modal, Form, Input, Select, Space, Tag, message,
  Popconfirm, Typography, Descriptions, Switch,
} from 'antd'
import {
  PlusOutlined, SyncOutlined, CheckCircleOutlined, DeleteOutlined,
  CloudServerOutlined, EyeOutlined, CopyOutlined, ExclamationCircleOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { accountService, AWSAccount, CreateAccountRequest } from '../../services/accounts'
import { useAccountStore } from '../../stores/accountStore'
import ResponsiveList from '../../components/ResponsiveList'
import styles from './Accounts.module.css'

const { Title } = Typography
const { Option } = Select
const { TextArea } = Input

// 复制到剪贴板的通用方法（兼容非 HTTPS 环境）
const copyToClipboard = (text: string) => {
  // 优先使用 navigator.clipboard（需要 HTTPS 或 localhost）
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => {
      message.success(t('common.copied'));
    }).catch(() => {
      fallbackCopy(text);
    });
  } else {
    fallbackCopy(text);
  }
};

// Fallback 复制方法（使用 textarea）
const fallbackCopy = (text: string) => {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    document.execCommand('copy');
    message.success(t('common.copied'));
  } catch {
    message.error(t('common.copyFailed') || '复制失败');
  }
  document.body.removeChild(textarea);
};

// SSO region 支持所有 AWS regions
const ssoRegions = [
  { value: 'us-east-1', labelKey: 'accounts.regionVirginia' },
  { value: 'us-east-2', label: 'Ohio' },
  { value: 'us-west-1', label: 'N. California' },
  { value: 'us-west-2', label: 'Oregon' },
  { value: 'eu-central-1', labelKey: 'accounts.regionFrankfurt' },
  { value: 'eu-central-2', label: 'Zurich' },
  { value: 'eu-west-1', label: 'Ireland' },
  { value: 'eu-west-2', label: 'London' },
  { value: 'eu-west-3', label: 'Paris' },
  { value: 'eu-south-1', label: 'Milan' },
  { value: 'eu-south-2', label: 'Spain' },
  { value: 'eu-north-1', label: 'Stockholm' },
  { value: 'ap-northeast-1', label: 'Tokyo' },
  { value: 'ap-northeast-2', label: 'Seoul' },
  { value: 'ap-northeast-3', label: 'Osaka' },
  { value: 'ap-southeast-1', label: 'Singapore' },
  { value: 'ap-southeast-2', label: 'Sydney' },
  { value: 'ap-south-1', label: 'Mumbai' },
  { value: 'ca-central-1', label: 'Canada Central' },
  { value: 'sa-east-1', label: 'São Paulo' },
]

// Kiro region 只支持弗吉尼亚和法兰克福
const kiroRegions = [
  { value: 'us-east-1', labelKey: 'accounts.regionVirginia' },
  { value: 'eu-central-1', labelKey: 'accounts.regionFrankfurt' },
]

export default function Accounts() {
  const { t } = useTranslation()
  const [accounts, setAccounts] = useState<AWSAccount[]>([])
  const [loading, setLoading] = useState(false)
  const [modalVisible, setModalVisible] = useState(false)
  const [detailAccount, setDetailAccount] = useState<AWSAccount | null>(null)
  const [form] = Form.useForm()
  const [verifying, setVerifying] = useState<number | null>(null)
  const [syncing, setSyncing] = useState<number | null>(null)
  const [permissionModalVisible, setPermissionModalVisible] = useState(false)
  const { fetchAccounts: refreshStore } = useAccountStore()

  useEffect(() => {
    loadAccounts()
  }, [])

  const loadAccounts = async () => {
    setLoading(true)
    try {
      const response = await accountService.list()
      setAccounts(response.accounts)
      refreshStore() // 刷新全局 store，让侧边栏菜单更新
    } catch (error) {
      message.error(t('accounts.loadFailed'))
    } finally {
      setLoading(false)
    }
  }

  const handleCreate = async (values: CreateAccountRequest) => {
    try {
      const account = await accountService.create(values)
      message.success(t('accounts.createdVerifying', { name: account.name }))
      setModalVisible(false)
      form.resetFields()
      await loadAccounts()

      // 自动 verify + sync
      try {
        const verifyResult = await accountService.verify(account.id)
        if (verifyResult.status === 'active') {
          message.success(t('accounts.verifiedSyncing'))
          const syncResult = await accountService.sync(account.id)
          message.success(t('accounts.syncSuccess', { users: syncResult.synced_users, subs: syncResult.synced_subscriptions }))
        } else {
          message.warning(t('accounts.verificationIssues', { msg: verifyResult.message }))
        }
        loadAccounts()
      } catch (e) {
        message.warning(t('accounts.autoVerifyFailed'))
      }
    } catch (error: any) {
      message.error(error.response?.data?.detail || t('accounts.createFailed'))
    }
  }

  const handleVerify = async (accountId: number) => {
    setVerifying(accountId)
    try {
      const result = await accountService.verify(accountId)
      if (result.status === 'active') {
        message.success(t('accounts.verifySuccess'))
      } else {
        message.warning(t('accounts.verificationIssues', { msg: result.message }))
      }
      loadAccounts()
    } catch (error) {
      message.error(t('accounts.verifyFailed'))
    } finally {
      setVerifying(null)
    }
  }

  const handleSync = async (accountId: number) => {
    setSyncing(accountId)
    try {
      const result = await accountService.sync(accountId)
      message.success(t('accounts.syncSuccess', { users: result.synced_users, subs: result.synced_subscriptions }))
      loadAccounts()
    } catch (error) {
      message.error(t('accounts.syncFailed'))
    } finally {
      setSyncing(null)
    }
  }

  const handleDelete = async (accountId: number) => {
    try {
      await accountService.delete(accountId)
      message.success(t('accounts.deleteSuccess'))
      loadAccounts()
    } catch (error) {
      message.error(t('accounts.deleteFailed'))
    }
  }

  const getStatusTag = (status: string) => {
    const config = {
      active: { color: 'success', icon: <CheckCircleOutlined /> },
      pending: { color: 'warning', icon: <SyncOutlined spin /> },
      invalid: { color: 'error', icon: null },
    }
    const c = config[status as keyof typeof config] || { color: 'default', icon: null }
    return <Tag color={c.color} icon={c.icon}>{status}</Tag>
  }

  const columns = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      width: 140,
      render: (name: string) => (
        <Space><CloudServerOutlined /><span>{name}</span></Space>
      ),
    },
    {
      title: t('accounts.kiroLoginUrl'),
      dataIndex: 'identity_store_id',
      key: 'kiro_url',
      render: (id: string) => {
        if (!id) return <Tag>{t('accounts.notConnected')}</Tag>;
        const url = `https://${id}.awsapps.com/start`;
        return (
          <Space size={4}>
            <a href={url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>{url}</a>
            <Button type="text" size="small" icon={<CopyOutlined />}
              onClick={() => copyToClipboard(url)} />
          </Space>
        );
      },
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: getStatusTag,
    },
    {
      title: t('accounts.autoSync'),
      dataIndex: 'sync_interval_minutes',
      key: 'sync_interval_minutes',
      width: 80,
      render: (val: number | undefined) => {
        if (!val || val <= 0) return <Tag>Off</Tag>
        return <Tag color="cyan">{val}min</Tag>
      },
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 220,
      render: (_: any, record: AWSAccount) => (
        <Space>
          <Button type="link" size="small" icon={<EyeOutlined />}
            onClick={() => setDetailAccount(record)}>{t('common.detail')}</Button>
          <Button type="link" size="small"
            onClick={() => handleVerify(record.id)}
            loading={verifying === record.id}>{t('accounts.verify')}</Button>
          {record.status === 'active' && (
            <Button type="link" size="small"
              onClick={() => handleSync(record.id)}
              loading={syncing === record.id}>{t('common.sync')}</Button>
          )}
          <Popconfirm title={t('accounts.deleteConfirm')} onConfirm={() => handleDelete(record.id)}>
            <Button type="link" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div className={styles.accounts}>
      <div className={styles.header}>
        <Title level={2}>{t('accounts.title')}</Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalVisible(true)}>
          {t('accounts.add')}
        </Button>
      </div>

      <Card>
        <ResponsiveList columns={columns} dataSource={accounts} rowKey="id" loading={loading}
          pagination={{ pageSize: 10 }} scroll={{ x: 900 }} />
      </Card>

      {/* 详情弹窗（含编辑） */}
      <Modal title={t('accounts.detail')} open={!!detailAccount} onCancel={() => setDetailAccount(null)}
        footer={
          <Space>
            <Button onClick={() => setDetailAccount(null)}>{t('common.close')}</Button>
            <Button type="primary" onClick={async () => {
              if (!detailAccount) return;
              const desc = (document.getElementById('edit-desc') as HTMLInputElement)?.value;
              const isDefault = (document.getElementById('edit-default') as HTMLInputElement)?.checked;
              try {
                await accountService.update(detailAccount.id, { description: desc ?? undefined, is_default: isDefault });
                message.success(t('common.success'));
                loadAccounts();
                setDetailAccount(null);
              } catch { message.error(t('accounts.saveFailed')); }
            }}>{t('common.save')}</Button>
          </Space>
        } width={640}>
        {detailAccount && (
          <Descriptions bordered column={1} size="small">
            <Descriptions.Item label={t('common.name')}>{detailAccount.name}</Descriptions.Item>
            <Descriptions.Item label={t('common.description')}>
              <Input id="edit-desc" defaultValue={detailAccount.description || ''} placeholder={t('common.description')} />
            </Descriptions.Item>
            <Descriptions.Item label={t('accounts.isDefault')}>
              <input id="edit-default" type="checkbox" defaultChecked={detailAccount.is_default} /> {t('accounts.isDefault')}
            </Descriptions.Item>
            <Descriptions.Item label={t('accounts.accessKeyId')}>{detailAccount.access_key_masked || '******'}</Descriptions.Item>
            <Descriptions.Item label={t('accounts.ssoRegion')}>{detailAccount.sso_region}</Descriptions.Item>
            <Descriptions.Item label={t('accounts.kiroRegion')}>{detailAccount.kiro_region}</Descriptions.Item>
            <Descriptions.Item label={t('common.status')}>{getStatusTag(detailAccount.status)}</Descriptions.Item>
            <Descriptions.Item label={t('accounts.kiroLoginUrl')}>
              {detailAccount.identity_store_id
                ? (() => {
                  const url = `https://${detailAccount.identity_store_id}.awsapps.com/start`;
                  return (
                    <Space>
                      <a href={url} target="_blank" rel="noreferrer">{url}</a>
                      <Button type="text" size="small" icon={<CopyOutlined />}
                        onClick={() => copyToClipboard(url)} />
                    </Space>
                  );
                })()
                : t('accounts.notConnected')}
            </Descriptions.Item>
            <Descriptions.Item label={t('accounts.identityStoreId')}>{detailAccount.identity_store_id || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('accounts.autoSync')}>
              {detailAccount.sync_interval_minutes
                ? t('accounts.autoSyncEvery', { min: detailAccount.sync_interval_minutes })
                : t('accounts.autoSyncDisabled')}
            </Descriptions.Item>
            <Descriptions.Item label={t('accounts.lastVerified')}>
              {detailAccount.last_synced ? new Date(detailAccount.last_synced).toLocaleString() : t('common.never')}
            </Descriptions.Item>
            <Descriptions.Item label={t('common.createdAt')}>
              {new Date(detailAccount.created_at).toLocaleString()}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Modal>

      {/* 添加账号弹窗 */}
      <Modal title={t('accounts.addTitle')} open={modalVisible}
        onCancel={() => { setModalVisible(false); form.resetFields() }}
        footer={null} width={600}>
        <Form form={form} layout="vertical" onFinish={handleCreate}
          initialValues={{ sso_region: 'us-east-1', kiro_region: 'us-east-1', sync_interval_minutes: 5, is_default: false }}>
          <Form.Item name="name" label={t('accounts.name')}
            rules={[{ required: true, message: t('accounts.accountNameRequired') }]}>
            <Input placeholder={t('accounts.accountNamePlaceholder')} />
          </Form.Item>
          <Form.Item name="description" label={t('common.description')}>
            <TextArea rows={2} placeholder={t('accounts.descriptionPlaceholder')} />
          </Form.Item>
          <Form.Item name="access_key_id"
            label={<Space>{t('accounts.accessKeyId')}<ExclamationCircleOutlined style={{ color: '#faad14', cursor: 'pointer' }} onClick={() => setPermissionModalVisible(true)} /></Space>}
            rules={[{ required: true, message: t('accounts.accessKeyRequired') }]}>
            <Input placeholder={t('accounts.accessKeyPlaceholder')} />
          </Form.Item>
          <Form.Item name="secret_access_key"
            label={<Space>{t('accounts.secretAccessKey')}<ExclamationCircleOutlined style={{ color: '#faad14', cursor: 'pointer' }} onClick={() => setPermissionModalVisible(true)} /></Space>}
            rules={[{ required: true, message: t('accounts.secretKeyRequired') }]}>
            <Input.Password placeholder={t('accounts.secretKeyPlaceholder')} />
          </Form.Item>
          <Space style={{ width: '100%' }} size="large">
            <Form.Item name="sso_region" label={t('accounts.ssoRegion')} style={{ marginBottom: 0, width: 240 }}>
              <Select showSearch optionFilterProp="children">
                {ssoRegions.map(r => <Option key={r.value} value={r.value}>{r.labelKey ? t(r.labelKey) : r.label} ({r.value})</Option>)}
              </Select>
            </Form.Item>
            <Form.Item name="kiro_region" label={t('accounts.kiroRegion')} style={{ marginBottom: 0, width: 240 }}>
              <Select>{kiroRegions.map(r => <Option key={r.value} value={r.value}>{t(r.labelKey)} ({r.value})</Option>)}</Select>
            </Form.Item>
          </Space>
          <Form.Item name="sync_interval_minutes" label={t('accounts.autoSync')} style={{ marginTop: 16 }}
            extra={t('accounts.autoSyncExtra')}>
            <Select>
              <Option value={0}>{t('accounts.autoSyncDisabled')}</Option>
              <Option value={3}>{t('accounts.autoSyncEvery', { min: 3 })}</Option>
              <Option value={5}>{t('accounts.autoSyncEvery', { min: 5 })}</Option>
              <Option value={15}>{t('accounts.autoSyncEvery', { min: 15 })}</Option>
              <Option value={30}>{t('accounts.autoSyncEvery', { min: 30 })}</Option>
              <Option value={60}>{t('accounts.autoSyncEvery', { min: 60 })}</Option>
            </Select>
          </Form.Item>
          <Form.Item name="is_default" label={t('accounts.isDefault')} valuePropName="checked" style={{ marginTop: 8 }}>
            <Switch />
          </Form.Item>
          <Form.Item style={{ marginTop: 24, marginBottom: 0 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => { setModalVisible(false); form.resetFields() }}>{t('common.cancel')}</Button>
              <Button type="primary" htmlType="submit">{t('accounts.addButton')}</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* IAM 权限说明弹窗 */}
      <Modal title={t('accounts.permissionTitle')} open={permissionModalVisible}
        onCancel={() => setPermissionModalVisible(false)}
        footer={<Button type="primary" onClick={() => setPermissionModalVisible(false)}>OK</Button>}
        width={680}>
        <div style={{ fontSize: 14, lineHeight: 1.8 }}>
          <p>{t('accounts.permissionIntro')}</p>

          <Title level={5}>1. {t('accounts.managedPolicy')}</Title>
          <Tag color="blue" style={{ fontSize: 13, padding: '2px 8px' }}>AWSSSOMasterAccountAdministrator</Tag>

          <Title level={5} style={{ marginTop: 16 }}>2. {t('accounts.customInlinePolicy')}</Title>
          <p style={{ color: '#ff4d4f' }}>
            {t('accounts.inlinePolicyWarning')}
          </p>
          <pre style={{
            background: '#f5f5f5', padding: 12, borderRadius: 6, fontSize: 13,
            overflow: 'auto', maxHeight: 300,
          }}>{`{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "user-subscriptions:*",
                "q:*"
            ],
            "Resource": "*"
        }
    ]
}`}</pre>

          <Title level={5} style={{ marginTop: 16 }}>{t('accounts.whyNeeded')}</Title>
          <ul style={{ paddingLeft: 20 }}>
            <li><code>user-subscriptions:*</code> — {t('accounts.permUserSub')}</li>
            <li><code>q:CreateAssignment</code> — {t('accounts.permCreateAssignment')}</li>
            <li><code>q:DeleteAssignment</code> — {t('accounts.permDeleteAssignment')}</li>
            <li><code>q:UpdateAssignment</code> — {t('accounts.permUpdateAssignment')}</li>
            <li><code>AWSSSOMasterAccountAdministrator</code> — {t('accounts.permSSO')}</li>
          </ul>
        </div>
      </Modal>
    </div>
  )
}
