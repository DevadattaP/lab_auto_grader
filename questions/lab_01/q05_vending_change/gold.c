#include<stdio.h>

int main()
{
    int price, amount, change;
    scanf("%d %d", &price, &amount);

    if(price < 0 || amount < 0)
    {
        printf("INVALID INPUT");
    }
    else if(amount < price)
    {
        printf("INSUFFICIENT FUNDS, need %d more", price - amount);
    }
    else if(amount == price)
    {
        printf("EXACT PAYMENT, ENJOY!");
    }
    else
    {
        change = amount - price;

        int c100 = change / 100;
        change = change % 100;
        if(c100 > 0) { printf("100 x %d\n", c100);}

        int c50 = change / 50;
        change = change % 50;
        if(c50 > 0) { printf("50 x %d\n", c50);}

        int c20 = change / 20;
        change = change % 20;
        if(c20 > 0) { printf("20 x %d\n", c20);}

        int c10 = change / 10;
        change = change % 10;
        if(c10 > 0) { printf("10 x %d\n", c10);}

        int c5 = change / 5;
        change = change % 5;
        if(c5 > 0) { printf("5 x %d\n", c5);}

        int c1 = change;
        if(c1 > 0) { printf("1 x %d", c1);}
    }

    return 0;
}
