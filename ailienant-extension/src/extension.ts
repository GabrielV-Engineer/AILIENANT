import * as vscode from 'vscode';
import { IntentRouter } from './core/IntentRouter';
import { SessionManager } from './brain/session';
import { AilienantChatProvider } from './providers/chat_sidebar';

export function activate(context: vscode.ExtensionContext) {
	console.log('Congratulations, your extension "ailienant-extension" is now active!');

	const helloWorld = vscode.commands.registerCommand('ailienant-extension.helloWorld', () => {
		vscode.window.showInformationMessage('Hello World from ailienant-extension!');
	});

	const runTask = vscode.commands.registerCommand('ailienant-extension.runTask', async () => {
		const prompt = await vscode.window.showInputBox({
			prompt: 'Enter your directive for AILIENANT',
			placeHolder: 'e.g. "format", "constify", or describe a complex task',
		});
		if (!prompt) { return; }
		const doc = vscode.window.activeTextEditor?.document;
		const intercepted = await IntentRouter.intercept(prompt, doc);
		if (!intercepted) {
			await SessionManager.getInstance().startAITask(prompt);
		}
	});

	const chatProvider = new AilienantChatProvider(context.extensionUri, context.workspaceState);
	const chatRegistration = vscode.window.registerWebviewViewProvider(
		AilienantChatProvider.viewType, chatProvider,
	);

	context.subscriptions.push(helloWorld, runTask, chatRegistration);
}

export function deactivate() {}
