'use client';

import { useEffect, useRef, useState } from 'react';

import { getWorkflowsApiV1WorkflowFetchGet, listFoldersApiV1FolderGet } from '@/client/sdk.gen';
import type { FolderResponse, WorkflowListResponse } from '@/client/types.gen';
import { CreateWorkflowButton } from '@/components/workflow/CreateWorkflowButton';
import { AgentFolderView } from '@/components/workflow/folders/AgentFolderView';
import { CreateFolderButton } from '@/components/workflow/folders/CreateFolderButton';
import { FolderSection } from '@/components/workflow/folders/FolderSection';
import { UploadWorkflowButton } from '@/components/workflow/UploadWorkflowButton';
import { detailFromError } from '@/lib/apiError';
import { useAuth } from '@/lib/auth';
import logger from '@/lib/logger';

/**
 * Client-side version of the workflow list for the embedded "Viato Voice"
 * deployment (AUTH_PROVIDER=clerk).
 *
 * The standalone /workflow page is a server component that reads the access token
 * from cookies — which the embed never sets (its Clerk token arrives client-side
 * over postMessage). This component replicates the same UI but fetches
 * client-side, so the API client's interceptor attaches the bridge token. The
 * non-clerk (local/stack) flow continues to use the server component unchanged.
 */
export default function EmbeddedWorkflowList() {
    const { user, loading: authLoading } = useAuth();
    const hasFetched = useRef(false);

    const [active, setActive] = useState<WorkflowListResponse[]>([]);
    const [archived, setArchived] = useState<WorkflowListResponse[]>([]);
    const [folders, setFolders] = useState<FolderResponse[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        // Wait for auth to be ready so the interceptor attaches the bridge token.
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;

        const fetchData = async () => {
            try {
                setIsLoading(true);
                setError(null);

                // Fetch both active and archived workflows in a single request.
                const response = await getWorkflowsApiV1WorkflowFetchGet({
                    query: { status: 'active,archived' },
                });

                if (response.error) {
                    setError(detailFromError(response.error, 'Failed to load Workflows. Please Try Again Later.'));
                    return;
                }

                const allWorkflowData = response.data ?? [];

                // Separate active and archived workflows, newest first.
                setActive(
                    allWorkflowData
                        .filter((w) => w.status === 'active')
                        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
                );
                setArchived(
                    allWorkflowData
                        .filter((w) => w.status === 'archived')
                        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
                );

                // Fetch folders for grouping active agents. A failure here shouldn't
                // break the page — fall back to an empty list (flat, ungrouped view).
                const foldersResponse = await listFoldersApiV1FolderGet();
                if (foldersResponse.error) {
                    logger.error(`Error fetching folders: ${detailFromError(foldersResponse.error)}`);
                    setFolders([]);
                } else {
                    setFolders(foldersResponse.data ?? []);
                }
            } catch (err) {
                logger.error(`Error fetching workflows: ${err}`);
                setError('Failed to load Workflows. Please Try Again Later.');
            } finally {
                setIsLoading(false);
            }
        };

        fetchData();
    }, [authLoading, user]);

    if (authLoading || isLoading) {
        return <WorkflowsLoading />;
    }

    return (
        <div className="container mx-auto px-4 py-8">
            {/* Your Workflows Section */}
            <div className="mb-6">
                <div className="flex justify-between items-center mb-6">
                    <h1 className="text-2xl font-bold">Your Agents</h1>
                    <div className="flex gap-2">
                        <UploadWorkflowButton />
                        <CreateFolderButton />
                        <CreateWorkflowButton />
                    </div>
                </div>

                {error ? (
                    <div className="text-red-500">{error}</div>
                ) : (
                    <>
                        {/* Active Workflows Section */}
                        <div className="mb-8">
                            <h2 className="text-xl font-semibold mb-4">Active Agents</h2>
                            {active.length > 0 || folders.length > 0 ? (
                                <AgentFolderView workflows={active} folders={folders} />
                            ) : (
                                <div className="text-muted-foreground bg-muted rounded-lg p-8 text-center">
                                    No active workflows found. Create your first workflow to get started.
                                </div>
                            )}
                        </div>

                        {/* Archived Section — collapsible, same design as the folder/Uncategorized sections */}
                        {archived.length > 0 && (
                            <div className="mb-8">
                                <FolderSection kind="archived" workflows={archived} />
                            </div>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}

function WorkflowsLoading() {
    return (
        <div className="container mx-auto px-4 py-8">
            {/* Get Started Section Loading */}
            <div className="mb-12">
                <div className="h-8 w-48 bg-muted rounded mb-6"></div>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    {Array.from({ length: 3 }, (_, i) => (
                        <div key={i} className="bg-muted rounded-lg h-40"></div>
                    ))}
                </div>
            </div>

            {/* Your Workflows Section Loading */}
            <div className="mb-6">
                <div className="flex justify-between items-center mb-6">
                    <div className="h-8 w-48 bg-muted rounded"></div>
                    <div className="h-10 w-32 bg-muted rounded"></div>
                </div>
                <div className="bg-muted rounded-lg h-96"></div>
            </div>
        </div>
    );
}
