from openai import OpenAI

client = OpenAI(
    api_key="sk-c02ed13a41a64f9f863301c9cb815e1c",
    base_url="https://api.deepseek.com",
)

response = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "你是谁"},
    ],
)

print(response.choices[0].message.content)
