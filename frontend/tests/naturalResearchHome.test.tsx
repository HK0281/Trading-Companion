import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import { ResearchController } from "../src/controller";
import { mockApi } from "./fixtures";
import { modeController } from "./modeFixtures";

async function normalWorkspace() {
  const fixture = modeController();
  render(<App controller={fixture.controller} />);
  await waitFor(() => expect(fixture.controller.snapshot().health).not.toBeNull());
  return fixture;
}

describe("natural single-stock research entry", () => {
  it("makes date-free research discoverable without creating or sending a request", async () => {
    const { normal, development } = await normalWorkspace();
    expect(screen.getByText("不用填写日期或指标")).toBeVisible();
    expect(screen.getByText(/直接说股票名称即可/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /个股研究/ }));
    expect(screen.getByLabelText("研究问题")).toHaveValue("分析平安银行最近的走势，关注趋势与动量");
    expect(normal.create).not.toHaveBeenCalled();
    expect(normal.send).not.toHaveBeenCalled();
    expect(development.health).not.toHaveBeenCalled();
  });

  it("fills an outlook question locally and sends its wording only after explicit submission", async () => {
    const { normal } = await normalWorkspace();
    fireEvent.click(screen.getByRole("button", { name: /后续展望/ }));
    expect(screen.getByLabelText("研究问题")).toHaveValue("分析平安银行下个月可能的走势");
    expect(normal.create).not.toHaveBeenCalled();
    expect(normal.send).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(normal.send).toHaveBeenCalledOnce());
    expect(normal.send.mock.calls[0][2]).toBe("分析平安银行下个月可能的走势");
    expect(normal.create).toHaveBeenCalledOnce();
    expect(await screen.findByText("历史数据：2026-09-21 — 2026-09-25")).toBeVisible();
    expect(screen.queryByRole("button", { name: /后续展望/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("候选股票")).not.toBeInTheDocument();
  });

  it.each(["分析一下中际旭创", "预测一下中际旭创下个月的走势"])("does not rewrite or require parameters for %s", async (question) => {
    const { normal } = await normalWorkspace();
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: question } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(normal.send).toHaveBeenCalledOnce());
    expect(normal.send.mock.calls[0][2]).toBe(question);
    expect(normal.create).toHaveBeenCalledOnce();
  });

  it("keeps the fixed dated research and edit examples for offline testing", async () => {
    const api = mockApi(), controller = new ResearchController(api);
    render(<App controller={controller} />);
    await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
    fireEvent.click(screen.getByRole("button", { name: /个股研究/ }));
    expect(screen.getByLabelText("研究问题")).toHaveValue("分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2");
    fireEvent.click(screen.getByRole("button", { name: /调整条件/ }));
    expect(screen.getByLabelText("研究问题")).toHaveValue("把SMA3改成5日，其他不变");
    expect(screen.queryByRole("button", { name: /后续展望/ })).not.toBeInTheDocument();
    expect(api.create).not.toHaveBeenCalled();
    expect(api.send).not.toHaveBeenCalled();
  });
});
