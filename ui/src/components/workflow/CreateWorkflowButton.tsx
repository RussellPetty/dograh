'use client';

import { Bot, ChevronDown, LayoutTemplate, PlusIcon } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { toast } from 'sonner';

import { createWorkflowApiV1WorkflowCreateDefinitionPost } from '@/client/sdk.gen';
import { Button } from "@/components/ui/button";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { detailFromError } from '@/lib/apiError';
import { useAuth } from '@/lib/auth';
import logger from '@/lib/logger';
import { getRandomId } from '@/lib/utils';

import { FolderFormDialog } from './folders/FolderFormDialog';

const BLANK_WORKFLOW_DEFINITION = {
    nodes: [
        {
            id: "1",
            type: "startCall",
            position: { x: 175, y: 60 },
            data: {
                prompt: "# Goal\nYou are a helpful agent who is handing a conversation over voice with a human. This is a voice conversation, so transcripts can be error prone.\n\n## Rules\n- Language: UK English but does not have to be correct english\n- Keep responses short and 2-3 sentences max\n- If you have to repeat something that you said in your previous two turns, then rephrase a bit while keeping the same meaning. Never repeat the exact same words as in your previous 2 responses.\n\n## Speech Handling\n- There could be multiple transcription errors. \n- Accept variations: yes/yeah/yep/aye, no/nah/nope\n- If user says \"sorry?\" or \"pardon me\" or \"can you repeat\"  or \"what?\", they might not have heard you- so just repeat what you just said.\n\n### Flow\nStart by saying \"Hi\". Be polite and courteous. ",
                name: "start call",
                greeting: "Hi! This is {{agent_name}} with {{company}}.",
                greeting_type: "text",
                allow_interrupt: true,
                invalid: false,
                validationMessage: null,
                add_global_prompt: false,
                delayed_start: false,
                is_start: true,
                selected_through_edge: false,
                hovered_through_edge: false,
                extraction_enabled: false,
                selected: false,
                dragging: false,
            },
        },
    ],
    edges: [],
    viewport: { x: 808, y: 269, zoom: 0.75 },
};

export function CreateWorkflowButton() {
    const router = useRouter();
    const { user, getAccessToken, provider } = useAuth();
    const [isCreating, setIsCreating] = useState(false);
    const [isNameDialogOpen, setIsNameDialogOpen] = useState(false);
    // Suggested default name, generated once per dialog open so it stays stable
    // across renders (FolderFormDialog resets its field whenever initialName
    // changes identity).
    const [defaultName, setDefaultName] = useState('');

    const handleAgentBuilder = () => {
        router.push('/workflow/create');
    };

    // Create a blank agent with the name the user typed. The name becomes the
    // workflow name, which also drives the {{agent_name}} greeting variable.
    // Throws on failure so FolderFormDialog keeps the dialog open (toast is
    // surfaced here, matching CreateFolderButton's pattern).
    const handleCreateNamed = async (name: string) => {
        if (!user) return;
        setIsCreating(true);
        try {
            const accessToken = await getAccessToken();
            const response = await createWorkflowApiV1WorkflowCreateDefinitionPost({
                body: {
                    name,
                    workflow_definition: BLANK_WORKFLOW_DEFINITION as unknown as { [key: string]: unknown },
                },
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                },
            });

            if (response.error) {
                const detail = detailFromError(response.error, 'Failed to create agent');
                toast.error(detail);
                throw new Error(detail);
            }
            if (response.data?.id) {
                router.push(`/workflow/${response.data.id}`);
            }
        } catch (err) {
            logger.error(`Error creating blank workflow: ${err}`);
            // A network failure (no response.error path) won't have shown a toast.
            if (!(err instanceof Error)) {
                toast.error('Failed to create agent');
            }
            throw err; // keep the dialog open
        } finally {
            setIsCreating(false);
        }
    };

    // Ask for a name first, then create. Pre-fill a sensible default the user can
    // accept or replace; an empty name is not allowed (enforced by the dialog's
    // trim check).
    const openNameDialog = () => {
        if (isCreating || !user) return;
        setDefaultName(`Agent-${getRandomId()}`);
        setIsNameDialogOpen(true);
    };

    const nameDialog = (
        <FolderFormDialog
            open={isNameDialogOpen}
            onOpenChange={setIsNameDialogOpen}
            title="Name your agent"
            label="Agent name"
            placeholder="e.g. Sales Outreach, Appointment Booker"
            initialName={defaultName}
            allowUnchanged
            submitLabel="Create Agent"
            onSubmit={handleCreateNamed}
        />
    );

    // The embedded "Viato Voice" (clerk) deployment doesn't use the MPS-backed
    // Agent Builder (a hosted dograh service that needs a per-user service key) —
    // create a blank agent locally and open the editor instead.
    if (provider === 'clerk') {
        return (
            <>
                <Button onClick={openNameDialog} disabled={isCreating || !user}>
                    <PlusIcon className="w-4 h-4" />
                    {isCreating ? 'Creating...' : 'Create Agent'}
                </Button>
                {nameDialog}
            </>
        );
    }

    return (
        <>
            <DropdownMenu>
                <DropdownMenuTrigger asChild>
                    <Button disabled={isCreating}>
                        <PlusIcon className="w-4 h-4" />
                        {isCreating ? 'Creating...' : 'Create Agent'}
                        <ChevronDown className="w-4 h-4" />
                    </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={handleAgentBuilder} className="cursor-pointer">
                        <Bot className="w-4 h-4 mr-2" />
                        <div>
                            <div className="font-medium">Use Agent Builder</div>
                            <div className="text-xs text-muted-foreground">AI generates a workflow from your description</div>
                        </div>
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={openNameDialog} disabled={isCreating} className="cursor-pointer">
                        <LayoutTemplate className="w-4 h-4 mr-2" />
                        <div>
                            <div className="font-medium">Blank Canvas</div>
                            <div className="text-xs text-muted-foreground">Start from scratch with an empty workflow</div>
                        </div>
                    </DropdownMenuItem>
                </DropdownMenuContent>
            </DropdownMenu>
            {nameDialog}
        </>
    );
}
