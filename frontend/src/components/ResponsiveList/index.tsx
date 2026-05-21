import React from 'react';
import { Table, Card, Pagination, Spin, Empty } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import styles from './ResponsiveList.module.css';

function useIsMobile() {
    const [isMobile, setIsMobile] = React.useState(window.innerWidth < 768);
    React.useEffect(() => {
        const handler = () => setIsMobile(window.innerWidth < 768);
        window.addEventListener('resize', handler);
        return () => window.removeEventListener('resize', handler);
    }, []);
    return isMobile;
}

interface ResponsiveListProps<T> {
    columns: ColumnsType<T>;
    dataSource: T[];
    rowKey: string | ((record: T) => string | number);
    loading?: boolean;
    pagination?: {
        total?: number;
        pageSize?: number;
        current?: number;
        showTotal?: (total: number) => string;
        onChange?: (page: number, pageSize: number) => void;
    } | false;
    scroll?: { x?: number };
    onChange?: (pagination: any, filters: any, sorter: any) => void;
    rowSelection?: any;
}

function ResponsiveList<T extends Record<string, any>>({
    columns,
    dataSource,
    rowKey,
    loading = false,
    pagination,
    scroll,
    onChange,
    rowSelection,
}: ResponsiveListProps<T>) {
    const isMobile = useIsMobile();

    const getKey = (record: T, index: number): string | number => {
        if (typeof rowKey === 'function') return rowKey(record);
        return record[rowKey] ?? index;
    };

    if (!isMobile) {
        return (
            <Table
                columns={columns}
                dataSource={dataSource}
                rowKey={rowKey as string}
                loading={loading}
                pagination={pagination}
                scroll={scroll}
                onChange={onChange}
                rowSelection={rowSelection}
            />
        );
    }

    // Mobile: Card list
    if (loading) {
        return <div style={{ textAlign: 'center', padding: 40 }}><Spin /></div>;
    }

    if (!dataSource.length) {
        return <Empty description="暂无数据" />;
    }

    // Filter out "actions" column for separate rendering
    const dataColumns = columns.filter(
        (col: any) => col.key !== 'actions' && col.dataIndex !== 'actions'
    );
    const actionsColumn = columns.find(
        (col: any) => col.key === 'actions' || col.dataIndex === 'actions'
    ) as any;

    return (
        <div className={styles.cardList}>
            {dataSource.map((record, index) => (
                <Card key={getKey(record, index)} size="small" className={styles.card}>
                    {dataColumns.map((col: any) => {
                        const value = col.dataIndex ? record[col.dataIndex] : undefined;
                        const rendered = col.render ? col.render(value, record, index) : value;
                        if (rendered === undefined || rendered === null || rendered === '' || rendered === '-') return null;
                        return (
                            <div key={col.key || col.dataIndex} className={styles.cardRow}>
                                <span className={styles.cardLabel}>{typeof col.title === 'string' ? col.title : ''}</span>
                                <span className={styles.cardValue}>{rendered}</span>
                            </div>
                        );
                    })}
                    {actionsColumn && (
                        <div className={styles.cardActions}>
                            {actionsColumn.render ? actionsColumn.render(undefined, record, index) : null}
                        </div>
                    )}
                </Card>
            ))}

            {pagination && (pagination.total ?? 0) > 0 && (
                <div className={styles.pagination}>
                    <Pagination
                        size="small"
                        current={pagination.current}
                        pageSize={pagination.pageSize}
                        total={pagination.total}
                        showTotal={pagination.showTotal}
                        onChange={pagination.onChange}
                        simple
                    />
                </div>
            )}
        </div>
    );
}

export default ResponsiveList;
